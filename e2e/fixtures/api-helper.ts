/**
 * Direct REST API client for verifications, async-wait polling, and teardown cleanup.
 */

const API_URL = process.env.E2E_API_URL || 'http://127.0.0.1:8000/api/v1';
const ADMIN_EMAIL = process.env.E2E_ADMIN_EMAIL || 'admin@localhost.test';
const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD || 'OpenCuria2026!';

let cachedToken: string | null = null;
let cachedOrgId: string | null = null;

async function getAuth(): Promise<{ token: string; orgId: string }> {
  if (cachedToken && cachedOrgId) return { token: cachedToken, orgId: cachedOrgId };

  const res = await fetch(`${API_URL}/auth/login/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: ADMIN_EMAIL, password: ADMIN_PASSWORD }),
  });
  if (!res.ok) throw new Error(`Login failed: ${res.status}`);
  const data = await res.json();
  cachedToken = data.access_token;

  // Get org id from /auth/me/
  const meRes = await fetch(`${API_URL}/auth/me/`, {
    headers: { Authorization: `Bearer ${cachedToken}` },
  });
  const me = await meRes.json();
  cachedOrgId = me.organizations?.[0]?.id;
  if (!cachedOrgId) throw new Error('No organization found');

  return { token: cachedToken!, orgId: cachedOrgId! };
}

async function apiRequest(method: string, path: string, body?: unknown): Promise<any> {
  const { token, orgId } = await getAuth();
  const res = await fetch(`${API_URL}${path}`, {
    method,
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
      'X-Organization-Id': orgId,
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return undefined;
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    return { _error: true, status: res.status, detail: data?.detail || 'Unknown error' };
  }
  return data;
}

export async function apiRequestWithKey(
  method: string,
  path: string,
  apiKey: string,
): Promise<{ status: number; data: any }> {
  const { orgId } = await getAuth();
  const res = await fetch(`${API_URL}${path}`, {
    method,
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${apiKey}`,
      'X-Organization-Id': orgId,
    },
  });
  const data = await res.json().catch(() => null);
  return { status: res.status, data };
}

export const api = {
  get: (path: string) => apiRequest('GET', path),
  post: (path: string, body?: unknown) => apiRequest('POST', path, body),
  patch: (path: string, body?: unknown) => apiRequest('PATCH', path, body),
  delete: (path: string) => apiRequest('DELETE', path),
};

/**
 * Poll an API endpoint until a condition is met.
 */
export async function pollUntil<T>(
  fetcher: () => Promise<T>,
  condition: (result: T) => boolean,
  opts: { interval?: number; timeout?: number } = {},
): Promise<T> {
  const interval = opts.interval || 3000;
  const timeout = opts.timeout || 180_000;
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const result = await fetcher();
    if (condition(result)) return result;
    await new Promise((r) => setTimeout(r, interval));
  }
  throw new Error(`pollUntil timed out after ${timeout}ms`);
}

/**
 * Wait for a workspace to reach a target status.
 */
export async function waitForWorkspaceStatus(
  workspaceId: string,
  targetStatus: string,
  timeoutMs = 180_000,
): Promise<any> {
  return pollUntil(
    () => api.get(`/workspaces/${workspaceId}/`),
    (ws: any) => ws.status === targetStatus,
    { interval: 3000, timeout: timeoutMs },
  );
}

/**
 * Wait for an image build to reach a terminal status.
 */
export async function waitForImageBuild(
  definitionId: string,
  runnerId: string,
  timeoutMs = 600_000,
): Promise<any> {
  return pollUntil(
    async () => {
      const builds = await api.get(`/image-definitions/${definitionId}/runner-builds/`);
      if (Array.isArray(builds)) {
        return builds.find((b: any) => b.runner_id === runnerId);
      }
      return builds;
    },
    (build: any) => build && ['active', 'failed'].includes(build.status),
    { interval: 5000, timeout: timeoutMs },
  );
}

/**
 * Cleanup helper: delete a resource, ignoring 404s.
 */
export async function safeDelete(path: string): Promise<void> {
  try {
    await apiRequest('DELETE', path);
  } catch {
    // ignore
  }
}

/**
 * Cleanup helper: stop a workspace first, then delete it.
 */
export async function safeStopAndDelete(workspaceId: string): Promise<void> {
  try {
    const ws = await api.get(`/workspaces/${workspaceId}/`);
    if (ws && !ws._error && ws.status === 'running') {
      await api.post(`/workspaces/${workspaceId}/stop/`);
      await pollUntil(
        () => api.get(`/workspaces/${workspaceId}/`),
        (w: any) => w.status !== 'running',
        { interval: 2000, timeout: 60_000 },
      ).catch(() => {});
    }
    await api.delete(`/workspaces/${workspaceId}/`);
  } catch {
    // ignore
  }
}
