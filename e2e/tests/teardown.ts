/**
 * Global teardown — safety net cleanup of any test resources left behind.
 * Runs after all specs complete (even on failures).
 */

const API_URL = process.env.E2E_API_URL || 'http://127.0.0.1:8000/api/v1';
const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL || 'admin@localhost.test';
const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD || 'OpenCuria2026!';

async function globalTeardown() {
  console.log('\n🧹 Global teardown: cleaning up any remaining e2e- resources...');

  try {
    // Login
    const loginRes = await fetch(`${API_URL}/auth/login/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: ADMIN_EMAIL, password: ADMIN_PASSWORD }),
    });
    const { access_token } = await loginRes.json();

    const meRes = await fetch(`${API_URL}/auth/me/`, {
      headers: { Authorization: `Bearer ${access_token}` },
    });
    const me = await meRes.json();
    const orgId = me.organizations?.[0]?.id;

    const headers = {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${access_token}`,
      'X-Organization-Id': orgId,
    };

    const get = async (path: string) => {
      const res = await fetch(`${API_URL}${path}`, { headers });
      return res.json();
    };

    const del = async (path: string) => {
      try {
        await fetch(`${API_URL}${path}`, { method: 'DELETE', headers });
      } catch {}
    };

    const post = async (path: string, body?: any) => {
      try {
        await fetch(`${API_URL}${path}`, {
          method: 'POST',
          headers,
          body: body ? JSON.stringify(body) : undefined,
        });
      } catch {}
    };

    // Find all e2e- prefixed resources
    const isE2e = (name: string) => name?.startsWith('e2e-');

    // 1. Workspaces: stop then delete
    const workspaces = await get('/workspaces/');
    if (Array.isArray(workspaces)) {
      for (const ws of workspaces.filter((w: any) => isE2e(w.name) && w.status !== 'removed')) {
        console.log(`  Cleaning workspace: ${ws.name} (${ws.id})`);
        if (ws.status === 'running') {
          await post(`/workspaces/${ws.id}/stop/`);
          await new Promise((r) => setTimeout(r, 5000));
        }
        await del(`/workspaces/${ws.id}/`);
      }
    }

    // 2. API Keys
    const apiKeys = await get('/auth/api-keys/');
    if (Array.isArray(apiKeys)) {
      for (const key of apiKeys.filter((k: any) => isE2e(k.name))) {
        console.log(`  Cleaning API key: ${key.name} (${key.id})`);
        await del(`/auth/api-keys/${key.id}/`);
      }
    }

    // 3. Skills
    const skills = await get('/skills/');
    if (Array.isArray(skills)) {
      for (const skill of skills.filter((s: any) => isE2e(s.name))) {
        console.log(`  Cleaning skill: ${skill.name} (${skill.id})`);
        await del(`/skills/${skill.id}/`);
      }
    }

    // 4. Credentials
    const creds = await get('/credentials/');
    if (Array.isArray(creds)) {
      for (const cred of creds.filter((c: any) => isE2e(c.name))) {
        console.log(`  Cleaning credential: ${cred.name} (${cred.id})`);
        await del(`/credentials/${cred.id}/`);
      }
    }

    // 5. Image definitions
    const imageDefs = await get('/image-definitions/');
    if (Array.isArray(imageDefs)) {
      for (const img of imageDefs.filter((i: any) => isE2e(i.name))) {
        console.log(`  Cleaning image def: ${img.name} (${img.id})`);
        await del(`/image-definitions/${img.id}/`);
      }
    }

    // 6. Agent definitions
    const agents = await get('/org-agent-definitions/');
    if (Array.isArray(agents)) {
      for (const agent of agents.filter((a: any) => isE2e(a.name))) {
        console.log(`  Cleaning agent def: ${agent.name} (${agent.id})`);
        await del(`/org-agent-definitions/${agent.id}/`);
      }
    }

    // 7. Image artifacts
    const artifacts = await get('/image-artifacts/');
    if (Array.isArray(artifacts)) {
      for (const art of artifacts.filter((a: any) => isE2e(a.name))) {
        console.log(`  Cleaning image artifact: ${art.name} (${art.id})`);
        await del(`/image-artifacts/${art.id}/`);
      }
    }

    // 8. Credential services — can only deactivate, no DELETE endpoint
    const credSvcs = await get('/org-credential-services/');
    if (Array.isArray(credSvcs)) {
      for (const svc of credSvcs.filter((s: any) => isE2e(s.name))) {
        console.log(`  Deactivating credential service: ${svc.name} (${svc.id})`);
        await post(`/org-credential-services/${svc.id}/activation/`, { active: false });
      }
    }

    console.log('🧹 Global teardown complete.');
  } catch (e) {
    console.error('Global teardown error:', e);
  }
}

export default globalTeardown;
