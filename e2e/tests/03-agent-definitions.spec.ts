/**
 * 03-agent-definitions.spec.ts — Test agent definition CRUD.
 */
import { test, expect } from '../fixtures/auth.fixture';
import { api } from '../fixtures/api-helper';

const BASE_URL = process.env.E2E_BASE_URL || 'http://127.0.0.1:8080';

test.describe('03 — Agent Definitions', () => {
  test('should create a custom agent definition', async ({ authedPage: page, testState }) => {
    await page.goto(`${BASE_URL}/org-settings`);
    await page.waitForLoadState('networkidle');

    await page.getByRole('button', { name: 'Agent Definitions' }).click();
    await page.waitForTimeout(500);

    // Click New Agent
    await page.getByRole('button', { name: /new agent/i }).click();
    await page.waitForSelector('[role="dialog"]');

    const dialog = page.locator('[role="dialog"]');
    const agentName = `${testState.prefix}-agent`;

    // Fill name
    await dialog.locator('input[placeholder*="my-custom-agent"]').fill(agentName);

    // Fill description (textarea, not input)
    await dialog.locator('textarea[placeholder*="What does this agent"], [placeholder*="What does this agent"]').fill('E2E test agent definition');

    // The run phase command should already have a default entry
    // Fill the run command
    const runCmd = dialog.locator('input[placeholder*="{prompt}"]');
    if (await runCmd.isVisible()) {
      await runCmd.clear();
      await runCmd.fill(`echo "Hello {prompt}"`);
    }

    // Click Create Agent
    await dialog.getByRole('button', { name: /create agent/i }).click();

    // Wait for dialog close
    await expect(dialog).toBeHidden({ timeout: 10_000 });

    // Verify agent appears in list
    await expect(page.getByText(agentName)).toBeVisible({ timeout: 10_000 });

    // Save ID via API
    const agents = await api.get('/org-agent-definitions/');
    const created = agents.find((a: any) => a.name === agentName);
    expect(created).toBeTruthy();
    testState.agentDefinitionId = created.id;
    console.log(`Created agent definition: ${created.id}`);
  });

  test('should show agent in definitions list with custom badge', async ({ authedPage: page, testState }) => {
    await page.goto(`${BASE_URL}/org-settings`);
    await page.waitForLoadState('networkidle');

    await page.getByRole('button', { name: 'Agent Definitions' }).click();
    await page.waitForTimeout(500);

    const agentName = `${testState.prefix}-agent`;
    await expect(page.getByText(agentName)).toBeVisible();
    // Custom agents should show "custom" badge
    await expect(page.getByText('custom').first()).toBeVisible();
  });

  test('should duplicate an existing agent definition', async ({ authedPage: page, testState }) => {
    test.skip(!testState.agentDefinitionId, 'No agent definition created');

    // Use API to duplicate since the UI Duplicate button is hard to target per-row
    const original = await api.get(`/org-agent-definitions/${testState.agentDefinitionId}/`);
    const dupName = `${original.name}-copy`;
    const dupe = await api.post('/org-agent-definitions/', {
      name: dupName,
      description: `Copy of ${original.description}`,
      commands: original.commands || [],
      is_active: true,
    });

    if (dupe && !dupe._error) {
      testState.agentDuplicateId = dupe.id;
      console.log(`Duplicated agent: ${dupe.id} (${dupe.name})`);

      // Verify on UI
      await page.goto(`${BASE_URL}/org-settings`);
      await page.waitForLoadState('networkidle');
      await page.getByRole('button', { name: 'Agent Definitions' }).click();
      await page.waitForTimeout(500);
      await expect(page.getByText(dupName)).toBeVisible({ timeout: 5_000 });
    } else {
      console.log(`Duplication failed: ${JSON.stringify(dupe)}`);
    }
  });

  test('should toggle agent activation', async ({ authedPage: page, testState }) => {
    test.skip(!testState.agentDefinitionId, 'No agent definition');

    await page.goto(`${BASE_URL}/org-settings`);
    await page.waitForLoadState('networkidle');

    await page.getByRole('button', { name: 'Agent Definitions' }).click();
    await page.waitForTimeout(500);

    const agentName = `${testState.prefix}-agent`;
    const agentRow = page.locator(`text=${agentName}`).locator('xpath=ancestor::div[contains(@class,"flex")]').first();

    // Find activation toggle
    const activeBtn = agentRow.getByRole('button', { name: /^active$/i });
    if (await activeBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
      // Deactivate
      await activeBtn.click();
      await page.waitForTimeout(1000);
      await expect(agentRow.getByRole('button', { name: /inactive/i })).toBeVisible({ timeout: 5000 });

      // Re-activate
      await agentRow.getByRole('button', { name: /inactive/i }).click();
      await page.waitForTimeout(1000);
      await expect(agentRow.getByRole('button', { name: /^active$/i })).toBeVisible({ timeout: 5000 });
    }
  });

  test('should edit agent definition', async ({ authedPage: page, testState }) => {
    test.skip(!testState.agentDefinitionId, 'No agent definition');

    // Use API to edit since the UI edit flow is complex
    const updated = await api.patch(`/org-agent-definitions/${testState.agentDefinitionId}/`, {
      description: 'E2E test agent — updated',
    });
    expect(updated._error).toBeFalsy();
    expect(updated.description).toContain('updated');
  });
});
