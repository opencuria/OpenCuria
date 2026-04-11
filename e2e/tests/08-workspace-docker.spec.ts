/**
 * 08-workspace-docker.spec.ts — Test Docker workspace lifecycle.
 */
import { test, expect } from '../fixtures/auth.fixture';
import { api, pollUntil, waitForWorkspaceStatus } from '../fixtures/api-helper';

const BASE_URL = process.env.E2E_BASE_URL || 'http://127.0.0.1:8080';

test.describe('08 — Docker Workspace Lifecycle', () => {
  test('should create a Docker workspace via UI', async ({ authedPage: page, testState }) => {
    test.skip(!testState.dockerRunnerId, 'No Docker runner');

    // Find a usable Docker image definition
    const defs = await api.get('/image-definitions/');
    let dockerImgId: string | undefined;
    let dockerImgName: string | undefined;

    // Prefer the existing "Local Docker Workspace" or similar that has an active build
    for (const d of defs) {
      if (d.runtime_type === 'docker' && d.is_active) {
        dockerImgId = d.id;
        dockerImgName = d.name;
        break;
      }
    }

    test.skip(!dockerImgId, 'No active Docker image definition');

    await page.goto(`${BASE_URL}/workspaces`);
    await page.waitForLoadState('networkidle');

    // Click Create Workspace
    await page.getByRole('button', { name: /create workspace/i }).click();
    await page.waitForSelector('[role="dialog"]');

    const dialog = page.locator('[role="dialog"]');
    const wsName = `${testState.prefix}-docker-ws`;
    testState.workspaceDockerName = wsName;

    // Fill name — textbox with placeholder "My workspace"
    const nameInput = dialog.getByRole('textbox', { name: /my workspace/i });
    if (await nameInput.isVisible({ timeout: 2000 }).catch(() => false)) {
      await nameInput.fill(wsName);
    } else {
      // Fallback: first textbox in dialog
      await dialog.getByRole('textbox').first().fill(wsName);
    }
    await page.waitForTimeout(300);

    // Image is a combobox - the default may already be the right one
    // Check if the current selection already shows a docker image
    const imageCombo = dialog.getByRole('combobox');
    if (await imageCombo.isVisible({ timeout: 2000 }).catch(() => false)) {
      const comboText = await imageCombo.textContent();
      console.log(`Image combo current text: ${comboText}`);
      // If it doesn't contain our desired image, try to select it
      if (dockerImgName && !comboText?.includes(dockerImgName)) {
        await imageCombo.click();
        await page.waitForTimeout(300);
        const imgOption = page.locator('[role="option"]').filter({ hasText: new RegExp(dockerImgName.substring(0, 20), 'i') }).first();
        if (await imgOption.isVisible({ timeout: 2000 }).catch(() => false)) {
          await imgOption.click();
        } else {
          // Just click the first docker option
          const firstOption = page.locator('[role="option"]').first();
          if (await firstOption.isVisible({ timeout: 1000 }).catch(() => false)) {
            await firstOption.click();
          }
        }
        await page.waitForTimeout(300);
      }
    }

    // Click Create button (text is just "Create", not "Create Workspace")
    const createBtn = dialog.getByRole('button', { name: /^create$/i });
    if (await createBtn.isEnabled({ timeout: 5000 }).catch(() => false)) {
      await createBtn.click();
    } else {
      // Fallback: look for any create-like button that is enabled
      const allBtns = dialog.getByRole('button');
      const btnCount = await allBtns.count();
      for (let i = 0; i < btnCount; i++) {
        const text = await allBtns.nth(i).textContent();
        if (text?.toLowerCase().includes('create') && await allBtns.nth(i).isEnabled()) {
          await allBtns.nth(i).click();
          break;
        }
      }
    }

    await expect(dialog).toBeHidden({ timeout: 15_000 });

    const workspaces = await pollUntil(
      () => api.get('/workspaces/'),
      (items: any[]) => Array.isArray(items) && items.some((w: any) => w.name === wsName),
      { interval: 2000, timeout: 60_000 },
    );
    const created = workspaces.find((w: any) => w.name === wsName);
    expect(created).toBeTruthy();

    testState.workspaceDockerId = created.id;
    console.log(`Created Docker workspace: ${created.id} (status: ${created.status})`);

    if (created.status !== 'running') {
      const ws = await waitForWorkspaceStatus(created.id, 'running', 120_000);
      console.log(`Workspace is now: ${ws.status}`);
      expect(ws.status).toBe('running');
    }
  });

  test('should show workspace in workspace list', async ({ authedPage: page, testState }) => {
    test.skip(!testState.workspaceDockerId, 'No Docker workspace');

    await page.goto(`${BASE_URL}/workspaces`);
    await page.waitForLoadState('networkidle');

    const wsName = testState.workspaceDockerName!;
    await expect(page.getByText(wsName)).toBeVisible({ timeout: 15_000 });
  });

  test('should open workspace detail page', async ({ authedPage: page, testState }) => {
    test.skip(!testState.workspaceDockerId, 'No Docker workspace');

    await page.goto(`${BASE_URL}/workspaces/${testState.workspaceDockerId}`);
    await page.waitForLoadState('networkidle');

    // Should show the workspace name or chat interface
    await page.waitForTimeout(2000);

    // The detail page should have some workspace info
    const wsName = testState.workspaceDockerName!;
    const hasContent = await page.getByText(wsName).isVisible({ timeout: 5_000 }).catch(() => false) ||
      await page.locator('textarea, [contenteditable]').isVisible({ timeout: 5_000 }).catch(() => false);
    expect(hasContent).toBe(true);
  });

  test('should stop the Docker workspace', async ({ testState }) => {
    test.skip(!testState.workspaceDockerId, 'No Docker workspace');

    const ws = await api.get(`/workspaces/${testState.workspaceDockerId}/`);
    if (ws.status === 'running') {
      await api.post(`/workspaces/${testState.workspaceDockerId}/stop/`);

      // Wait for stopped status
      const stopped = await waitForWorkspaceStatus(testState.workspaceDockerId!, 'stopped', 60_000);
      expect(stopped.status).toBe('stopped');
      console.log('Workspace stopped successfully');
    } else {
      console.log(`Workspace already in status: ${ws.status}`);
    }
  });

  test('should resume the Docker workspace', async ({ testState }) => {
    test.skip(!testState.workspaceDockerId, 'No Docker workspace');

    const ws = await api.get(`/workspaces/${testState.workspaceDockerId}/`);
    if (ws.status === 'stopped') {
      await api.post(`/workspaces/${testState.workspaceDockerId}/resume/`);

      // Wait for running
      const running = await waitForWorkspaceStatus(testState.workspaceDockerId!, 'running', 120_000);
      expect(running.status).toBe('running');
      console.log('Workspace resumed successfully');
    }
  });

  test('should stop workspace again for cleanup later', async ({ testState }) => {
    test.skip(!testState.workspaceDockerId, 'No Docker workspace');

    const ws = await api.get(`/workspaces/${testState.workspaceDockerId}/`);
    if (ws.status === 'running') {
      await api.post(`/workspaces/${testState.workspaceDockerId}/stop/`);
      await waitForWorkspaceStatus(testState.workspaceDockerId!, 'stopped', 60_000);
    }
  });
});
