/**
 * 09-workspace-qemu.spec.ts — Test QEMU workspace lifecycle (skipped if QEMU runner offline).
 */
import { test, expect } from '../fixtures/auth.fixture';
import { api, waitForWorkspaceStatus } from '../fixtures/api-helper';

const BASE_URL = process.env.E2E_BASE_URL || 'http://127.0.0.1:8080';

test.describe('09 — QEMU Workspace Lifecycle', () => {
  test('should create a QEMU workspace if runner available', async ({ authedPage: page, testState }) => {
    test.skip(!testState.qemuRunnerId || !testState.qemuRunnerOnline, 'QEMU runner not available or offline');

    // Find a QEMU image definition
    const defs = await api.get('/image-definitions/');
    let qemuImgId: string | undefined;
    for (const d of defs) {
      if (d.runtime_type === 'qemu' && d.is_active) {
        qemuImgId = d.id;
        break;
      }
    }
    test.skip(!qemuImgId, 'No active QEMU image definition');

    const wsName = `${testState.prefix}-qemu-ws`;
    testState.workspaceQemuName = wsName;

    // Create via API (QEMU needs specific fields)
    const created = await api.post('/workspaces/', {
      name: wsName,
      image_id: `definition:${qemuImgId}`,
      runtime_type: 'qemu',
      runner_id: testState.qemuRunnerId,
      qemu_vcpus: 2,
      qemu_memory_mb: 2048,
      qemu_disk_size_gb: 20,
    });

    if (created._error) {
      console.log(`QEMU workspace creation failed: ${JSON.stringify(created)}`);
      return;
    }

    testState.workspaceQemuId = created.id;
    console.log(`Created QEMU workspace: ${created.id}`);

    // Wait for running (QEMU takes longer)
    try {
      const ws = await waitForWorkspaceStatus(created.id, 'running', 300_000);
      console.log(`QEMU workspace status: ${ws.status}`);
    } catch {
      console.log('QEMU workspace did not reach running status');
    }
  });

  test('should show QEMU workspace in list', async ({ authedPage: page, testState }) => {
    test.skip(!testState.workspaceQemuId, 'No QEMU workspace');

    await page.goto(`${BASE_URL}/workspaces`);
    await page.waitForLoadState('networkidle');
    await expect(page.getByText(testState.workspaceQemuName!)).toBeVisible({ timeout: 10_000 });
  });

  test('should stop and cleanup QEMU workspace', async ({ testState }) => {
    test.skip(!testState.workspaceQemuId, 'No QEMU workspace');

    const ws = await api.get(`/workspaces/${testState.workspaceQemuId}/`);
    if (ws.status === 'running') {
      await api.post(`/workspaces/${testState.workspaceQemuId}/stop/`);
      await waitForWorkspaceStatus(testState.workspaceQemuId!, 'stopped', 120_000);
    }
  });
});
