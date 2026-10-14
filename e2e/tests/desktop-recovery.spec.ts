import { test, expect } from '@playwright/test';

const BASE_URL = process.env.E2E_BASE_URL || 'http://127.0.0.1:5173';
const browserName = process.env.DESKTOP_RECOVERY_BROWSER === 'webkit' ? 'webkit' : 'chromium';
test.use({
  baseURL: BASE_URL,
  browserName,
  headless: true,
  launchOptions: browserName === 'chromium' ? { executablePath: '/usr/bin/google-chrome' } : {},
});

type ViewerIntent = { viewer_client_id: string; intent_revision: number };
type Lease = ViewerIntent & { state: 'held' | 'released' | 'expired' };
type ApiCall = {
  method: string;
  path: string;
  url?: string;
  intent?: ViewerIntent;
  status?: { leaseState: string; revision: number | null; epoch: string };
};
type StatusBehavior = 'first-get-503' | 'own-held-until-third';
type LeaseRecoveryBehavior = 'expired' | 'epoch-changed';
type ViewerBehavior = {
  connectDelayMs?: number;
  postMessage?: boolean;
  failFirstBoots?: number;
  failEveryBoot?: boolean;
};

type DiagnosticWindow = Window & {
  __iframeBoots?: number;
  __iframeRequests?: number;
  __iframeUrls?: string[];
  __desktopDiagnostic?: {
    openSidePanel: () => void;
    closeSidePanel: () => void;
    openModal: () => void;
    closeModal: () => void;
    switchWorkspace: (workspaceId: string) => void;
    snapshot: () => {
      workspaceId: string | null;
      proxyUrl: string | null;
      isConnected: boolean;
      isConnecting: boolean;
    };
    emit: (event: string, data: unknown) => void;
  };
  __savedDesktopWindow?: Window;
};

const diagnosticHtml = `<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>Desktop recovery diagnostic</title></head>
  <body style="margin:0;font:14px sans-serif">
    <style>
      [data-testid="side-panel-desktop"] { display:flex; flex-direction:column; height:100%; min-height:0; }
      [data-testid="side-panel-desktop"] > div:last-child { position:relative; height:560px; min-height:0; }
      [data-testid="side-panel-desktop-host"] { position:absolute; inset:0; min-height:560px; }
    </style>
    <div id="diagnostic-app"></div>
    <script type="module" src="/src/diagnostic-module.js"></script>
  </body>
</html>`;

const socketModule = `
const listeners = new Map();
export function onEvent(event, handler) {
  const handlers = listeners.get(event) || new Set();
  handlers.add(handler);
  listeners.set(event, handlers);
  return () => handlers.delete(handler);
}
export function emitTestEvent(event, data) {
  for (const handler of listeners.get(event) || []) handler(data);
}
`;

async function installDiagnosticPage(
  page: import('@playwright/test').Page,
  apiCalls: ApiCall[],
  options: {
    holdWorkspaceAStatus?: Promise<void>;
    holdWorkspaceAStart?: Promise<void>;
    statusBehavior?: StatusBehavior;
    leaseRecoveryBehavior?: LeaseRecoveryBehavior;
    viewerBehavior?: ViewerBehavior;
  } = {},
): Promise<void> {
  // Use the exact optimized Vue/Pinia module URLs the running Vite server
  // emits for the real components. This avoids duplicate Vue runtimes and
  // avoids depending on Vite's changing optimized-dependency hash.
  const [surfaceResponse, storeResponse] = await Promise.all([
    fetch(`${BASE_URL}/src/components/workspaces/DesktopSurface.vue`),
    fetch(`${BASE_URL}/src/stores/desktop.ts`),
  ]);
  expect(surfaceResponse.ok, 'Vite must serve DesktopSurface.vue').toBeTruthy();
  expect(storeResponse.ok, 'Vite must serve the desktop store').toBeTruthy();
  const surfaceSource = await surfaceResponse.text();
  const storeSource = await storeResponse.text();
  const vueUrl = surfaceSource.match(/(["'])(\/node_modules\/\.vite\/deps\/vue\.js\?v=[^"']+)\1/)?.[2];
  const piniaUrl = storeSource.match(/(["'])(\/node_modules\/\.vite\/deps\/pinia\.js\?v=[^"']+)\1/)?.[2];
  expect(vueUrl, 'Vite Vue dependency import').toBeTruthy();
  expect(piniaUrl, 'Vite Pinia dependency import').toBeTruthy();

  const diagnosticModule = `
import { createApp, h, ref } from ${JSON.stringify(vueUrl)};
import { createPinia } from ${JSON.stringify(piniaUrl)};
import DesktopSurface from '/src/components/workspaces/DesktopSurface.vue';
import SidePanelDesktop from '/src/components/workspaces/SidePanelDesktop.vue';
import WorkspaceDesktop from '/src/components/workspaces/WorkspaceDesktop.vue';
import { useDesktopStore } from '/src/stores/desktop.ts';
import { useSidePanelStore } from '/src/stores/sidePanel.ts';
import { emitTestEvent } from '/src/services/socket.ts';

const app = createApp({
  setup() {
    const workspaceId = ref('A');
    const desktop = useDesktopStore();
    const panel = useSidePanelStore();
    const snapshot = () => ({
      workspaceId: desktop.workspaceId,
      proxyUrl: desktop.proxyUrl,
      isConnected: desktop.isConnected,
      isConnecting: desktop.isConnecting,
    });
    window.__desktopDiagnostic = {
      openSidePanel: () => panel.open('desktop'),
      closeSidePanel: () => panel.close(),
      openModal: () => desktop.open(),
      closeModal: () => desktop.close(),
      switchWorkspace: (id) => { workspaceId.value = id; },
      snapshot,
      emit: emitTestEvent,
    };
    return () => h('main', {
      style: { display: 'flex', minHeight: '100vh', position: 'relative' },
    }, [
      h('nav', { style: { position: 'fixed', top: '0', left: '0', zIndex: '100' } }, [
        h('button', { 'data-testid': 'open-side-panel', onClick: () => panel.open('desktop') }, 'Open sidebar'),
        h('button', { 'data-testid': 'close-side-panel', onClick: () => panel.close() }, 'Close sidebar'),
        h('button', { 'data-testid': 'open-desktop-modal', onClick: () => desktop.open() }, 'Open modal'),
      ]),
      h('section', {
        'data-testid': 'diagnostic-sidebar',
        style: {
          display: panel.isOpen ? 'block' : 'none',
          width: '440px',
          height: '600px',
          marginTop: '40px',
        },
      }, panel.hasOpened
        ? [h(SidePanelDesktop, { key: workspaceId.value, workspaceId: workspaceId.value })]
        : []),
      h(WorkspaceDesktop, { workspaceId: workspaceId.value }),
      h(DesktopSurface, { key: workspaceId.value, workspaceId: workspaceId.value }),
    ]);
  },
});
app.use(createPinia());
app.mount('#diagnostic-app');
`;

  await page.route('**/desktop-diagnostic', (route) => route.fulfill({
    status: 200,
    contentType: 'text/html; charset=utf-8',
    body: diagnosticHtml,
  }));
  await page.route('**/src/diagnostic-module.js', (route) => route.fulfill({
    status: 200,
    contentType: 'text/javascript; charset=utf-8',
    body: diagnosticModule,
  }));
  await page.route('**/src/services/socket.ts*', (route) => route.fulfill({
    status: 200,
    contentType: 'text/javascript; charset=utf-8',
    body: socketModule,
  }));

  // The Kasm viewer is always local and deterministic; it never reaches a
  // runner. Parent counters expose iframe requests and successful document boots.
  let iframeRequests = 0;
  let disconnectObserved = false;
  let runnerEpoch = 'mock-runner-epoch';
  let leaseRecoveryTriggered = false;
  let failAllViewerBoots = Boolean(options.viewerBehavior?.failEveryBoot);
  const leases = new Map<string, Lease>();
  const statusGets = new Map<string, number>();
  const key = (workspace: string, client: string) => `${workspace}:${client}`;

  await page.route('**/__test/disconnect-observed', async (route) => {
    disconnectObserved = true;
    await route.fulfill({ status: 204, body: '' });
  });
  await page.route('**/__test/fail-viewer', async (route) => {
    failAllViewerBoots = true;
    await route.fulfill({ status: 204, body: '' });
  });
  await page.route('**/__test/allow-viewer', async (route) => {
    failAllViewerBoots = false;
    await route.fulfill({ status: 204, body: '' });
  });

  await page.route('**/mock-desktop/**', (route) => {
    iframeRequests += 1;
    const requestUrl = new URL(route.request().url());
    apiCalls.push({
      method: route.request().method(),
      path: requestUrl.pathname,
      url: requestUrl.href,
    });
    const failed = failAllViewerBoots ||
      iframeRequests <= (options.viewerBehavior?.failFirstBoots ?? 0);
    const connected = options.viewerBehavior?.connectDelayMs ?? 0;
    const postMessage = options.viewerBehavior?.postMessage !== false;
    return route.fulfill({
      status: failed ? 503 : 200,
      contentType: 'text/html; charset=utf-8',
      body: `<!doctype html><html><head><meta charset="utf-8"></head>
<body><main id="viewer">Mock desktop viewer</main>
<script>
  const root = document.documentElement;
  window.parent.__iframeRequests = ${iframeRequests};
  window.parent.__iframeBoots = (window.parent.__iframeBoots || 0) + 1;
  window.addEventListener('mock-desktop-disconnect', () => {
    root.classList.remove('noVNC_connected');
    ${postMessage ? "window.parent.postMessage({ action: 'connection_state', value: 'disconnected' }, '*');" : ''}
  });
  ${failed ? '' : `setTimeout(() => {
    root.classList.add('noVNC_connected');
    ${postMessage ? "window.parent.postMessage({ action: 'connection_state', value: 'connected' }, '*');" : ''}
  }, ${connected});`}
</script></body></html>`,
    });
  });

  // Explicitly block all backend REST and websocket traffic. The REST routes
  // below are a complete in-browser mock, including generic API calls.
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const method = request.method();
    const url = new URL(request.url());
    const path = url.pathname;
    const intent = method === 'POST' ? request.postDataJSON() as ViewerIntent : undefined;
    apiCalls.push({ method, path, intent });
    const statusMatch = path.match(/\/workspaces\/([^/]+)\/desktop\/status\/?$/);
    if (method === 'GET' && statusMatch) {
      const workspace = statusMatch[1];
      const client = url.searchParams.get('viewer_client_id');
      expect(client).toMatch(/^[\da-f]{8}(-[\da-f]{4}){3}-[\da-f]{12}$/);
      const getCount = (statusGets.get(workspace) ?? 0) + 1;
      statusGets.set(workspace, getCount);
      if (workspace === 'A' && options.statusBehavior === 'first-get-503' && getCount === 1) {
        await route.fulfill({ status: 503, contentType: 'application/json',
          body: JSON.stringify({ detail: 'Deterministic temporary status outage' }) });
        return;
      }
      // Capture the observation before delaying it to exercise stale responses.
      const lease = leases.get(key(workspace, client!));
      const awaitingRouting = options.statusBehavior === 'own-held-until-third' &&
        workspace === 'A' && getCount < 3;
      const leaseExpired = options.leaseRecoveryBehavior !== undefined && disconnectObserved &&
        !leaseRecoveryTriggered && workspace === 'A';
      if (leaseExpired) leaseRecoveryTriggered = true;
      if (leaseExpired && options.leaseRecoveryBehavior === 'expired') {
        leases.set(key(workspace, client!), {
          viewer_client_id: client!,
          intent_revision: lease?.intent_revision ?? 1,
          state: 'expired',
        });
      }
      if (leaseExpired && options.leaseRecoveryBehavior === 'epoch-changed')
        runnerEpoch = 'mock-runner-epoch-rotated';
      const staleLeaseState = leaseExpired && options.leaseRecoveryBehavior === 'expired'
        ? 'expired' : lease?.state ?? 'unknown';
      const observation = {
        active: !awaitingRouting,
        proxy_url: awaitingRouting ? null : `/mock-desktop/${workspace}`,
        viewer_held: [...leases.values()].some((entry) => entry.state === 'held'),
        computer_use_active: false, mcp_active: true, holder_count: 2,
        viewer_lease_state: staleLeaseState,
        revision: lease?.intent_revision ?? null,
        epoch: runnerEpoch,
      };
      apiCalls[apiCalls.length - 1]!.status = {
        leaseState: observation.viewer_lease_state,
        revision: observation.revision,
        epoch: observation.epoch ?? 'mock-runner-epoch',
      };
      if (workspace === 'A' && options.holdWorkspaceAStatus) await options.holdWorkspaceAStatus;
      await route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify(observation) });
      return;
    }
    const actionMatch = path.match(/\/workspaces\/([^/]+)\/desktop\/(stop\/|renew\/)?$/);
    if (method === 'POST' && actionMatch) {
      const workspace = actionMatch[1];
      const action = actionMatch[2] ?? 'start';
      expect(intent?.viewer_client_id).toMatch(/^[\da-f]{8}(-[\da-f]{4}){3}-[\da-f]{12}$/);
      expect(Number.isInteger(intent?.intent_revision)).toBe(true);
      expect(intent!.intent_revision).toBeGreaterThan(0);
      if (workspace === 'A' && action === 'start' && options.holdWorkspaceAStart)
        await options.holdWorkspaceAStart;
      const leaseKey = key(workspace, intent!.viewer_client_id);
      const previous = leases.get(leaseKey);
      if (action === 'stop/') {
        if (!previous || intent!.intent_revision >= previous.intent_revision)
          leases.set(leaseKey, { ...intent!, state: 'released' });
      } else if (action === 'start') {
        // The delayed start cannot resurrect an equal-revision tombstone.
        if (!previous || intent!.intent_revision > previous.intent_revision)
          leases.set(leaseKey, { ...intent!, state: 'held' });
      } else if (action === 'renew/') {
        if (previous?.state === 'expired') {
          await route.fulfill({ status: 409, contentType: 'application/json',
            body: JSON.stringify({ detail: 'Viewer lease expired' }) });
          return;
        }
        expect(previous?.state).toBe('held');
        expect(previous?.intent_revision).toBe(intent!.intent_revision);
      }
      await route.fulfill({ status: action === 'renew/' ? 200 : 202,
        contentType: 'application/json', body: JSON.stringify({
          task_id: `mock-${action}-${workspace}`,
          viewer_lease_state: leases.get(leaseKey)?.state,
          revision: leases.get(leaseKey)?.intent_revision, epoch: 'mock-runner-epoch',
        }) });
      return;
    }

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ task_id: 'mock-task', results: [] }),
    });
  });
  await page.route('**/config.json', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ apiBaseUrl: '/api/v1', wsBaseUrl: '' }),
  }));
  await page.route('**/ws/**', (route) => route.abort());
  await page.routeWebSocket(/\/ws\//, async (socket) => {
    await socket.close({ code: 1001, reason: 'Blocked by desktop recovery E2E' });
  });

  await page.goto('/desktop-diagnostic');
  await expect(page.getByTestId('open-side-panel')).toBeVisible();
}

async function emitDisconnect(page: import('@playwright/test').Page): Promise<void> {
  await page.locator('[data-testid="desktop-surface-iframe"]').evaluate((iframe) => {
    const frame = iframe as HTMLIFrameElement;
    frame.contentWindow?.dispatchEvent(new Event('mock-desktop-disconnect'));
  });
  await expect.poll(() => page.evaluate(() =>
    (window as DiagnosticWindow).__desktopDiagnostic?.snapshot().isConnected,
  )).toBe(true);
  await page.evaluate(async () => {
    const response = await fetch('/__test/disconnect-observed', { method: 'POST' });
    if (!response.ok) throw new Error('Could not arm deterministic lease transition');
  });
}

test.describe('desktop viewer recovery regression', () => {
  test.beforeEach(({ browser }) => {
    expect(browser.browserType().name()).toBe(browserName);
  });

  test('keeps one live iframe through sidebar/modal visibility and retries a disconnect', async ({ page }) => {
    const apiCalls: ApiCall[] = [];
    await installDiagnosticPage(page, apiCalls);

    await page.getByTestId('open-side-panel').click();

    const iframe = page.locator('[data-testid="desktop-surface-iframe"]');
    await expect(iframe).toBeVisible();
    await expect(page.getByTestId('desktop-surface-loading')).toHaveCount(0);
    await expect.poll(() => page.evaluate(() => (window as DiagnosticWindow).__iframeBoots ?? 0))
      .toBe(1);
    await page.evaluate(() => {
      (window as DiagnosticWindow).__savedDesktopWindow =
        document.querySelector<HTMLIFrameElement>('[data-testid="desktop-surface-iframe"]')?.contentWindow ?? undefined;
    });

    const assertSameViewer = async (): Promise<void> => {
      await expect.poll(() => page.evaluate(() => ({
        sameWindow: document.querySelector<HTMLIFrameElement>(
          '[data-testid="desktop-surface-iframe"]',
        )?.contentWindow === (window as DiagnosticWindow).__savedDesktopWindow,
        boots: (window as DiagnosticWindow).__iframeBoots ?? 0,
      }))).toEqual({ sameWindow: true, boots: 1 });
    };

    expect(apiCalls.filter((call) => call.method === 'POST' && /\/A\/desktop\/$/.test(call.path))).toHaveLength(1);
    await page.getByTestId('open-desktop-modal').click();
    await expect(page.getByTestId('workspace-desktop-modal')).toBeVisible();
    await assertSameViewer();
    expect(apiCalls.filter((call) => call.method === 'POST' && /\/A\/desktop\/$/.test(call.path))).toHaveLength(1);
    await page.getByTestId('desktop-modal-close').click();
    await expect(page.getByTestId('workspace-desktop-modal')).toHaveCount(0);
    await assertSameViewer();

    await page.getByTestId('close-side-panel').click();
    await expect(page.getByTestId('diagnostic-sidebar')).toBeHidden();
    await assertSameViewer();
    await page.getByTestId('open-side-panel').click();
    await expect(page.getByTestId('diagnostic-sidebar')).toBeVisible();
    await assertSameViewer();
    await expect(page.getByTestId('desktop-surface-loading')).toHaveCount(0);

    await emitDisconnect(page);
    await expect(page.getByTestId('desktop-surface-loading'))
      .toContainText('Desktop disconnected. Reconnecting…');
    const disconnectAt = Date.now();
    await expect.poll(() => page.evaluate(() => (window as DiagnosticWindow).__iframeBoots ?? 0), {
      timeout: 8_000,
      intervals: [50, 100, 200, 300],
    }).toBeGreaterThan(1);
    expect(Date.now() - disconnectAt).toBeGreaterThanOrEqual(900);
    await expect(page.getByTestId('desktop-surface-loading')).toHaveCount(0);
    await expect.poll(() => page.evaluate(() => (window as DiagnosticWindow).__iframeBoots ?? 0))
      .toBe(2);

    expect(apiCalls.some((call) => call.path.startsWith('/api/'))).toBeTruthy();
  });

  test('releases A on workspace switch and ignores its delayed status after B is connected', async ({ page }) => {
    const apiCalls: ApiCall[] = [];
    let releaseAStatus!: () => void;
    const holdWorkspaceAStatus = new Promise<void>((resolve) => {
      releaseAStatus = resolve;
    });
    await installDiagnosticPage(page, apiCalls, { holdWorkspaceAStatus });
    await page.getByTestId('open-side-panel').click();

    await expect.poll(() => apiCalls.filter((call) =>
      call.method === 'GET' && /\/workspaces\/A\/desktop\/status\/$/.test(call.path),
    ).length).toBe(1);
    await page.evaluate(() => (window as DiagnosticWindow).__desktopDiagnostic?.switchWorkspace('B'));

    await expect.poll(() => apiCalls.some((call) =>
      call.method === 'POST' && /\/workspaces\/A\/desktop\/stop\/$/.test(call.path),
    )).toBeTruthy();
    await expect.poll(() => apiCalls.filter((call) =>
      call.method === 'POST' && /\/workspaces\/B\/desktop\/$/.test(call.path),
    ).length).toBe(1);

    await expect.poll(() => page.evaluate(() =>
      (window as DiagnosticWindow).__desktopDiagnostic?.snapshot(),
    )).toEqual({
      workspaceId: 'B',
      proxyUrl: '/mock-desktop/B',
      isConnected: true,
      isConnecting: false,
    });

    releaseAStatus();
    await expect.poll(() => apiCalls.filter((call) =>
      call.method === 'GET' && /\/workspaces\/A\/desktop\/status\/$/.test(call.path),
    ).length).toBe(2);
    await page.waitForTimeout(150);

    expect(apiCalls.filter((call) =>
      call.method === 'POST' && /\/workspaces\/A\/desktop\/$/.test(call.path),
    )).toHaveLength(1);
    expect(apiCalls.filter((call) =>
      call.method === 'POST' && /\/workspaces\/B\/desktop\/$/.test(call.path),
    )).toHaveLength(1);
    await expect.poll(() => page.evaluate(() =>
      (window as DiagnosticWindow).__desktopDiagnostic?.snapshot(),
    )).toEqual({
      workspaceId: 'B',
      proxyUrl: '/mock-desktop/B',
      isConnected: true,
      isConnecting: false,
    });
    await expect(page.getByTestId('desktop-surface-loading')).toHaveCount(0);
  });

  test('tombstones an in-flight A start immediately and preserves B', async ({ page }) => {
    const apiCalls: ApiCall[] = [];
    let releaseAStart!: () => void;
    const holdWorkspaceAStart = new Promise<void>((resolve) => {
      releaseAStart = resolve;
    });
    await installDiagnosticPage(page, apiCalls, { holdWorkspaceAStart });
    await page.getByTestId('open-side-panel').click();

    // Ensure the auto-start POST has entered its held route before switching.
    await expect.poll(() => apiCalls.filter((call) =>
      call.method === 'POST' && /\/workspaces\/A\/desktop\/$/.test(call.path),
    ).length).toBe(1);

    const startAResponse = page.waitForResponse((response) =>
      response.request().method() === 'POST' &&
      /\/workspaces\/A\/desktop\/$/.test(new URL(response.url()).pathname),
    );
    await page.evaluate(() => (window as DiagnosticWindow).__desktopDiagnostic?.switchWorkspace('B'));

    await expect.poll(() => apiCalls.filter((call) =>
      call.method === 'POST' && /\/workspaces\/B\/desktop\/$/.test(call.path),
    ).length).toBe(1);

    await expect.poll(() => page.evaluate(() =>
      (window as DiagnosticWindow).__desktopDiagnostic?.snapshot(),
    )).toEqual({
      workspaceId: 'B',
      proxyUrl: '/mock-desktop/B',
      isConnected: true,
      isConnecting: false,
    });

    // Release is immediate. The durable same-revision tombstone must fence
    // the delayed start rather than waiting for transport acknowledgement.
    // Release is expressed as an equal-revision tombstone in the coordinator;
    // in-flight transport ordering remains separately checked after releasing.
    const aStart = apiCalls.find((call) => call.method === 'POST' && /\/A\/desktop\/$/.test(call.path))!;
    expect(aStart.intent?.intent_revision).toBeGreaterThan(0);

    releaseAStart();
    await startAResponse;
    await expect.poll(() => apiCalls.filter((call) =>
      call.method === 'POST' && /\/workspaces\/A\/desktop\/stop\/$/.test(call.path),
    ).length).toBeLessThanOrEqual(1);

    // Allow any erroneous duplicate teardown to surface, then verify A was
    // started/stopped once and the late A completion did not replace B.
    await page.waitForTimeout(200);
    expect(apiCalls.filter((call) =>
      call.method === 'POST' && /\/workspaces\/A\/desktop\/$/.test(call.path),
    )).toHaveLength(1);
    expect(apiCalls.filter((call) =>
      call.method === 'POST' && /\/workspaces\/B\/desktop\/$/.test(call.path),
    )).toHaveLength(1);
    expect(apiCalls.some((call) =>
      call.method === 'POST' && /\/workspaces\/B\/desktop\/stop\/$/.test(call.path),
    )).toBe(false);
    await expect.poll(() => page.evaluate(() =>
      (window as DiagnosticWindow).__desktopDiagnostic?.snapshot(),
    )).toEqual({
      workspaceId: 'B',
      proxyUrl: '/mock-desktop/B',
      isConnected: true,
      isConnecting: false,
    });
  });

  test('recovers a first status 503 without page reload, viewer remount, or duplicate start in under 5s', async ({ page }) => {
    const apiCalls: ApiCall[] = [];
    await installDiagnosticPage(page, apiCalls, { statusBehavior: 'first-get-503' });
    let mainFrameNavigations = 0;
    page.on('framenavigated', (frame) => {
      if (frame === page.mainFrame()) mainFrameNavigations += 1;
    });
    const startedAt = Date.now();
    await page.getByTestId('open-side-panel').click();
    await expect(page.getByTestId('desktop-surface-loading')).toHaveCount(0, { timeout: 5_000 });
    await expect.poll(() => page.evaluate(() => (window as DiagnosticWindow).__iframeBoots ?? 0))
      .toBe(1);
    expect(Date.now() - startedAt).toBeLessThan(5_000);
    expect(mainFrameNavigations).toBe(0);
    expect(apiCalls.filter((call) => call.method === 'POST' && /\/workspaces\/A\/desktop\/$/.test(call.path)))
      .toHaveLength(1);
  });

  test('keeps observing an owned lease while routing is inactive, then connects by the third status in under 5s', async ({ page }) => {
    const apiCalls: ApiCall[] = [];
    await installDiagnosticPage(page, apiCalls, { statusBehavior: 'own-held-until-third' });
    const startedAt = Date.now();
    await page.getByTestId('open-side-panel').click();
    await expect.poll(() => apiCalls.filter((call) =>
      call.method === 'GET' && /\/workspaces\/A\/desktop\/status\/$/.test(call.path),
    ).length, { timeout: 4_500 }).toBeGreaterThanOrEqual(3);
    await expect(page.getByTestId('desktop-surface-loading')).toHaveCount(0, { timeout: 4_500 });
    expect(Date.now() - startedAt).toBeLessThan(5_000);
    expect(apiCalls.filter((call) => call.method === 'POST' && /\/workspaces\/A\/desktop\/$/.test(call.path)))
      .toHaveLength(1);
    expect(await page.evaluate(() => (window as DiagnosticWindow).__iframeBoots ?? 0)).toBe(1);
    expect(await page.evaluate(() => (window as DiagnosticWindow).__desktopDiagnostic?.snapshot()))
      .toEqual({ workspaceId: 'A', proxyUrl: '/mock-desktop/A', isConnected: true, isConnecting: false });
  });

  for (const recoveryBehavior of ['expired', 'epoch-changed'] as const) {
    test(`reconciles ${recoveryBehavior} after viewer disconnect before booting revision 2`, async ({ page }) => {
      const apiCalls: ApiCall[] = [];
      await installDiagnosticPage(page, apiCalls, { leaseRecoveryBehavior: recoveryBehavior });
      await page.getByTestId('open-side-panel').click();
      await expect(page.getByTestId('desktop-surface-loading')).toHaveCount(0);
      const initialStart = apiCalls.find((call) =>
        call.method === 'POST' && /\/workspaces\/A\/desktop\/$/.test(call.path),
      );
      expect(initialStart?.intent).toBeTruthy();

      let mainFrameNavigations = 0;
      page.on('framenavigated', (frame) => {
        if (frame === page.mainFrame()) mainFrameNavigations += 1;
      });
      await emitDisconnect(page);
      await expect(page.getByTestId('desktop-surface-loading'))
        .toContainText('Desktop disconnected. Reconnecting…');

      await expect.poll(() => apiCalls.some((call) =>
        call.method === 'GET' && call.status?.epoch === 'mock-runner-epoch-rotated',
      ) || apiCalls.some((call) =>
        call.method === 'GET' && call.status?.leaseState === 'expired',
      )).toBeTruthy();
      const terminalStatusIndex = apiCalls.findIndex((call) =>
        call.method === 'GET' && (recoveryBehavior === 'expired'
          ? call.status?.leaseState === 'expired'
          : call.status?.epoch === 'mock-runner-epoch-rotated'),
      );
      await expect.poll(() => apiCalls.some((call) =>
        call.method === 'GET' && /\/workspaces\/A\/desktop\/status\/$/.test(call.path) &&
        (recoveryBehavior === 'expired'
          ? call.status?.leaseState === 'expired'
          : call.status?.epoch === 'mock-runner-epoch-rotated'),
      )).toBeTruthy();
      await expect.poll(() => apiCalls.some((call) =>
        call.path === '/mock-desktop/A' && call.url &&
        new URL(call.url).searchParams.get('intent_revision') === '2',
      )).toBeTruthy();

      const revisionTwoBoot = apiCalls.findIndex((call) =>
        call.path === '/mock-desktop/A' && call.url &&
        new URL(call.url).searchParams.get('intent_revision') === '2',
      );
      expect(terminalStatusIndex).toBeGreaterThanOrEqual(0);
      expect(revisionTwoBoot).toBeGreaterThan(terminalStatusIndex);
      expect(mainFrameNavigations).toBe(0);
      const starts = apiCalls.filter((call) =>
        call.method === 'POST' && /\/workspaces\/A\/desktop\/$/.test(call.path),
      );
      expect(starts.map((call) => call.intent?.intent_revision)).toEqual([
        initialStart!.intent!.intent_revision,
        initialStart!.intent!.intent_revision + 1,
      ]);
      await expect(page.getByTestId('desktop-surface-loading')).toHaveCount(0, { timeout: 5_000 });
      expect(await page.evaluate(() => (window as DiagnosticWindow).__desktopDiagnostic?.snapshot()))
        .toEqual({ workspaceId: 'A', proxyUrl: '/mock-desktop/A', isConnected: true, isConnecting: false });
      });
  }
  test('retries an iframe HTTP 503 automatically', async ({ page }) => {
    test.setTimeout(40_000);
    const apiCalls: ApiCall[] = [];
    await installDiagnosticPage(page, apiCalls, {
      viewerBehavior: { failFirstBoots: 1 },
    });
    await page.getByTestId('open-side-panel').click();
    await expect.poll(() => page.evaluate(() => (window as DiagnosticWindow).__iframeRequests ?? 0), {
      timeout: 5_000,
    }).toBe(1);
    await expect.poll(() => page.evaluate(() => (window as DiagnosticWindow).__iframeRequests ?? 0), {
      timeout: 35_000,
      intervals: [100, 250, 500, 1_000],
    }).toBeGreaterThan(1);
    await expect(page.getByTestId('desktop-surface-loading')).toHaveCount(0, { timeout: 10_000 });
    expect(apiCalls.filter((call) => call.method === 'POST' && /\/workspaces\/A\/desktop\/$/.test(call.path)))
      .toHaveLength(1);
  });

  test('exhausts three failed viewer boots, shows failure, and succeeds after manual Retry', async ({ page }) => {
    test.setTimeout(75_000);
    const apiCalls: ApiCall[] = [];
    await installDiagnosticPage(page, apiCalls, { viewerBehavior: { failEveryBoot: true } });
    await page.clock.install({ time: new Date('2026-01-01T00:00:00Z') });
    await page.getByTestId('open-side-panel').click();

    await expect.poll(() => page.evaluate(() => (window as DiagnosticWindow).__iframeRequests ?? 0))
      .toBe(1);
    await page.clock.runFor(15_000);
    await expect.poll(() => page.evaluate(() => (window as DiagnosticWindow).__iframeRequests ?? 0))
      .toBe(2);
    await page.clock.runFor(15_000);
    await expect.poll(() => page.evaluate(() => (window as DiagnosticWindow).__iframeRequests ?? 0))
      .toBe(3);
    await page.clock.runFor(15_000);
    await expect.poll(() => page.evaluate(() => (window as DiagnosticWindow).__iframeRequests ?? 0))
      .toBe(4);
    await page.clock.runFor(4_000);
    await expect(page.getByTestId('desktop-surface-loading'))
      .toContainText('Desktop connection failed.');
    await expect(page.getByTestId('desktop-surface-retry')).toBeVisible();
    expect(await page.evaluate(() => (window as DiagnosticWindow).__iframeRequests)).toBe(4);
    expect(apiCalls.filter((call) => call.method === 'POST' && /\/workspaces\/A\/desktop\/$/.test(call.path)))
      .toHaveLength(1);

    await page.evaluate(() => fetch('/__test/allow-viewer', { method: 'POST' }));
    await page.getByTestId('desktop-surface-retry').evaluate((button) => {
      (button as HTMLButtonElement).click();
    });
    await expect(page.getByTestId('desktop-surface-loading')).toHaveCount(0);
    expect(await page.evaluate(() => (window as DiagnosticWindow).__iframeRequests)).toBe(5);
    expect(apiCalls.filter((call) => call.method === 'POST' && /\/workspaces\/A\/desktop\/$/.test(call.path)))
      .toHaveLength(1);
  });

  test('reveals a delayed noVNC DOM connection without remounting when no message is sent', async ({ page }) => {
    const apiCalls: ApiCall[] = [];
    await installDiagnosticPage(page, apiCalls, {
      viewerBehavior: { connectDelayMs: 300, postMessage: false },
    });
    await page.getByTestId('open-side-panel').click();
    const iframe = page.locator('[data-testid="desktop-surface-iframe"]');
    await expect(iframe).toBeVisible();
    await expect.poll(() => page.evaluate(() => (window as DiagnosticWindow).__iframeBoots ?? 0))
      .toBe(1);
    await page.evaluate(() => {
      (window as DiagnosticWindow).__savedDesktopWindow =
        document.querySelector<HTMLIFrameElement>('[data-testid="desktop-surface-iframe"]')?.contentWindow ?? undefined;
    });
    let mainFrameNavigations = 0;
    page.on('framenavigated', (frame) => {
      if (frame === page.mainFrame()) mainFrameNavigations += 1;
    });
    const initialIframeUrl = await iframe.getAttribute('src');
    await expect(page.getByTestId('desktop-surface-loading')).toContainText('Connecting to desktop…');
    await expect.poll(() => iframe.evaluate((element) =>
      Boolean((element as HTMLIFrameElement).contentDocument?.documentElement.classList.contains('noVNC_connected')),
    )).toBe(true);
    await expect(page.getByTestId('desktop-surface-loading')).toHaveCount(0);
    expect(mainFrameNavigations).toBe(0);
    expect(await iframe.getAttribute('src')).toBe(initialIframeUrl);
    // The iframe must remain mounted; the DOM class can become true after load
    // without the versioned connection-state postMessage.
    await expect.poll(() => page.evaluate(() => ({
      sameWindow: document.querySelector<HTMLIFrameElement>(
        '[data-testid="desktop-surface-iframe"]',
      )?.contentWindow === (window as DiagnosticWindow).__savedDesktopWindow,
      boots: (window as DiagnosticWindow).__iframeBoots ?? 0,
    }))).toEqual({ sameWindow: true, boots: 1 });
  });
});
