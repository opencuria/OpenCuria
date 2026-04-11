/**
 * 99-cleanup.spec.ts — Delete all test resources in reverse order.
 * This also tests all delete/revoke endpoints.
 */
import { test, expect } from '../fixtures/auth.fixture';
import { ApiRequestError, api, safeDelete, safeStopAndDelete } from '../fixtures/api-helper';
import { loadState, resetState } from '../fixtures/test-context';

const BASE_URL = process.env.E2E_BASE_URL || 'http://127.0.0.1:8080';

test.describe('99 — Cleanup', () => {
  test('should delete Docker workspace', async ({ testState }) => {
    if (!testState.workspaceDockerId) {
      console.log('No Docker workspace to clean up');
      return;
    }
    await safeStopAndDelete(testState.workspaceDockerId);
    console.log(`Deleted Docker workspace: ${testState.workspaceDockerId}`);

    // Verify it's gone or in terminal status
    await new Promise(r => setTimeout(r, 3000));
    try {
      const ws = await api.get(`/workspaces/${testState.workspaceDockerId}/`);
      const isGone = ws.status === 'removed' || ws.status === 'deleting' || ws.status === 'stopped';
      expect(isGone).toBeTruthy();
    } catch (error) {
      expect(error).toBeInstanceOf(ApiRequestError);
      expect((error as ApiRequestError).status).toBe(404);
    }
  });

  test('should delete QEMU workspace', async ({ testState }) => {
    if (!testState.workspaceQemuId) {
      console.log('No QEMU workspace to clean up');
      return;
    }
    await safeStopAndDelete(testState.workspaceQemuId);
    console.log(`Deleted QEMU workspace: ${testState.workspaceQemuId}`);
  });

  test('should delete cloned workspace if exists', async ({ testState }) => {
    if (!testState.clonedWorkspaceId) {
      console.log('No cloned workspace to clean up');
      return;
    }
    await safeStopAndDelete(testState.clonedWorkspaceId);
    console.log(`Deleted cloned workspace: ${testState.clonedWorkspaceId}`);
  });

  test('should revoke API key via UI', async ({ authedPage: page, testState }) => {
    if (!testState.apiKeyId) {
      console.log('No API key to revoke');
      return;
    }

    await api.delete(`/auth/api-keys/${testState.apiKeyId}/`);
    console.log(`Deleted API key: ${testState.apiKeyId}`);
  });

  test('should delete skills', async ({ testState }) => {
    if (testState.skillPersonalId) {
      await safeDelete(`/skills/${testState.skillPersonalId}/`);
      console.log(`Deleted personal skill: ${testState.skillPersonalId}`);
    }
    if (testState.skillOrgId) {
      await safeDelete(`/skills/${testState.skillOrgId}/`);
      console.log(`Deleted org skill: ${testState.skillOrgId}`);
    }

    // Verify skills are gone
    const skills = await api.get('/skills/');
    const prefix = loadState().prefix;
    const remaining = skills.filter((s: any) => s.name?.startsWith(prefix));
    expect(remaining.length).toBe(0);
  });

  test('should delete credentials', async ({ testState }) => {
    for (const id of [testState.credentialEnvId, testState.credentialSshId, testState.credentialOrgId]) {
      if (id) {
        await safeDelete(`/credentials/${id}/`);
        console.log(`Deleted credential: ${id}`);
      }
    }

    // Verify
    const creds = await api.get('/credentials/');
    const prefix = loadState().prefix;
    const remaining = creds.filter((c: any) => c.name?.startsWith(prefix));
    expect(remaining.length).toBe(0);
  });

  test('should delete image definitions', async ({ testState }) => {
    if (testState.imageDefinitionDockerId) {
      await safeDelete(`/image-definitions/${testState.imageDefinitionDockerId}/`);
      console.log(`Deleted Docker image def: ${testState.imageDefinitionDockerId}`);
    }
    if (testState.imageDefinitionQemuId) {
      await safeDelete(`/image-definitions/${testState.imageDefinitionQemuId}/`);
      console.log(`Deleted QEMU image def: ${testState.imageDefinitionQemuId}`);
    }
  });

  test('should delete agent definitions', async ({ testState }) => {
    if (testState.agentDuplicateId) {
      await safeDelete(`/org-agent-definitions/${testState.agentDuplicateId}/`);
      console.log(`Deleted duplicate agent: ${testState.agentDuplicateId}`);
    }
    if (testState.agentDefinitionId) {
      await safeDelete(`/org-agent-definitions/${testState.agentDefinitionId}/`);
      console.log(`Deleted agent definition: ${testState.agentDefinitionId}`);
    }

    // Verify
    const agents = await api.get('/org-agent-definitions/');
    const prefix = loadState().prefix;
    const remaining = agents.filter((a: any) => a.name?.startsWith(prefix));
    expect(remaining.length).toBe(0);
  });

  test('should deactivate credential service', async ({ testState }) => {
    if (!testState.credentialServiceId) {
      console.log('No credential service to clean up');
      return;
    }

    // There's no DELETE endpoint for credential services — only deactivation
    const res = await api.post(`/org-credential-services/${testState.credentialServiceId}/activation/`, {
      active: false,
    });
    expect(res.is_active).toBe(false);
    console.log(`Deactivated credential service: ${testState.credentialServiceId}`);
  });

  test('should delete captured images if any', async ({ testState }) => {
    if (testState.capturedImageDockerId) {
      await safeDelete(`/image-artifacts/${testState.capturedImageDockerId}/`);
      console.log(`Deleted captured image: ${testState.capturedImageDockerId}`);
    }
  });

  test('should verify no test resources remain', async () => {
    const state = loadState();
    const prefix = state.prefix;

    const [agents, imageDefs, skills, creds, apiKeys, workspaces, credentialServices] = await Promise.all([
      api.get('/org-agent-definitions/'),
      api.get('/image-definitions/'),
      api.get('/skills/'),
      api.get('/credentials/'),
      api.get('/auth/api-keys/'),
      api.get('/workspaces/'),
      api.get('/org-credential-services/'),
    ]);

    const check = (items: any[], label: string, extraFilter?: (i: any) => boolean) => {
      if (!Array.isArray(items)) return 0;
      let filtered = items.filter((i: any) => i.name?.startsWith(prefix));
      if (extraFilter) filtered = filtered.filter(extraFilter);
      if (filtered.length > 0) {
        console.warn(`${label}: ${filtered.length} test items remaining`);
      }
      return filtered.length;
    };

    let total = 0;
    total += check(agents, 'Agent definitions');
    total += check(imageDefs, 'Image definitions');
    total += check(skills, 'Skills');
    total += check(creds, 'Credentials');
    total += check(credentialServices, 'Credential services', (svc: any) => svc.is_active !== false);
    // API keys: DELETE endpoint only revokes (is_active=false), doesn't remove from list
    total += check(apiKeys, 'API keys', (k: any) => k.is_active !== false);
    total += check(workspaces.filter((w: any) => !['removed', 'deleting', 'stopped'].includes(w.status)), 'Workspaces');

    expect(total).toBe(0);
    console.log('✅ All test resources cleaned up successfully');
  });

  test('should reset test state', async () => {
    resetState();
    console.log('Test state reset');
  });
});
