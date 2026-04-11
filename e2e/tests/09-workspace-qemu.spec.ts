/**
 * 09-workspace-qemu.spec.ts — Test QEMU workspace lifecycle (skipped if QEMU runner offline).
 */
import { test, expect } from '../fixtures/auth.fixture';
import { api, waitForWorkspaceStatus } from '../fixtures/api-helper';

const BASE_URL = process.env.E2E_BASE_URL || 'http://127.0.0.1:8080';

test.describe('09 — QEMU Workspace Lifecycle', () => {
  test('should create a QEMU workspace if runner available', async ({ authedPage: page, testState }) => {
    test.setTimeout(420_000); // QEMU VMs take longer to boot
    test.skip(!testState.qemuRunnerId || !testState.qemuRunnerOnline, 'QEMU runner not available or offline');

    // Find a QEMU image artifact to use — either from our build or any existing one
    let artifactId: string | undefined = testState.qemuImageArtifactId;

    if (!artifactId) {
      // Search for any active QEMU build with an artifact
      const defs = await api.get('/image-definitions/');
      for (const d of defs) {
        if (d.runtime_type !== 'qemu' || !d.is_active) continue;
        const builds = await api.get(`/image-definitions/${d.id}/runner-builds/`);
        if (Array.isArray(builds)) {
          const active = builds.find((b: any) =>
            b.status === 'active' &&
            b.image_artifact_id &&
            b.runner_id === testState.qemuRunnerId
          );
          if (active) {
            artifactId = active.image_artifact_id;
            console.log(`Using existing QEMU artifact: ${artifactId} (from ${d.name})`);
            break;
          }
        }
      }
    }

    test.skip(!artifactId, 'No QEMU image artifact available — build may not have completed');

    const wsName = `${testState.prefix}-qemu-ws`;
    testState.workspaceQemuName = wsName;

    // Create via API with minimal resources (1 vCPU, 1024 MB RAM, 20 GB disk)
    const created = await api.post('/workspaces/', {
      name: wsName,
      image_artifact_id: artifactId,
      runtime_type: 'qemu',
      runner_id: testState.qemuRunnerId,
      qemu_vcpus: 1,
      qemu_memory_mb: 1024,
      qemu_disk_size_gb: 20,
    });

    testState.workspaceQemuId = created.workspace_id || created.id;
    console.log(`Created QEMU workspace: ${testState.workspaceQemuId}`);

    const ws = await waitForWorkspaceStatus(testState.workspaceQemuId!, 'running', 300_000);
    console.log(`QEMU workspace status: ${ws.status}`);
    expect(ws.status).toBe('running');
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
