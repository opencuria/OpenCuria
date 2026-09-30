import { mkdirSync } from 'node:fs';
import { test, expect } from '@playwright/test';

const BASE_URL = process.env.E2E_BASE_URL || 'http://127.0.0.1:5173';
const browserName = process.env.DESKTOP_RECOVERY_BROWSER === 'webkit' ? 'webkit' : 'chromium';
const screenshotDir = '/workspace/.opencuria/playwright';

mkdirSync(screenshotDir, { recursive: true });

test.use({
  baseURL: BASE_URL,
  browserName,
  headless: true,
  launchOptions: browserName === 'chromium' ? { executablePath: '/usr/bin/google-chrome' } : {},
});

type ApiCall = { method: string; path: string };
type DiagnosticWindow = Window & {
  __iframeBoots?: number;
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
    connectMock: (workspaceId: string, proxyUrl: string) => void;
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
      connectMock: (id, proxyUrl) => {
        desktop.setConnected(id, proxyUrl);
        desktop.setComputerUseActive(false);
      },
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
  // runner. A parent-page counter makes actual iframe reloads observable.
  await page.route('**/mock-desktop/**', (route) => route.fulfill({
    status: 200,
    contentType: 'text/html; charset=utf-8',
    body: `<!doctype html><html><head><meta charset="utf-8"></head>
<body><main id="viewer">Mock desktop viewer</main>
<script>
  const root = document.documentElement;
  window.parent.__iframeBoots = (window.parent.__iframeBoots || 0) + 1;
  root.classList.add('noVNC_connected');
  window.addEventListener('mock-desktop-disconnect', () => {
    root.classList.remove('noVNC_connected');
    window.parent.postMessage({ action: 'connection_state', value: 'disconnected' }, '*');
  });
  window.parent.postMessage({ action: 'connection_state', value: 'connected' }, '*');
</script></body></html>`,
  }));

  // Explicitly block all backend REST and websocket traffic. The REST routes
  // below are a complete in-browser mock, including generic API calls.
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const method = request.method();
    const path = new URL(request.url()).pathname;
    apiCalls.push({ method, path });

    if (method === 'GET' && /\/workspaces\/A\/desktop\/status\/$/.test(path)) {
      if (options.holdWorkspaceAStatus) await options.holdWorkspaceAStatus;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          active: true,
          proxy_url: '/mock-desktop/A',
          viewer_held: true,
          computer_use_active: false,
        }),
      });
      return;
    }
    if (method === 'GET' && /\/workspaces\/[^/]+\/desktop\/status\/$/.test(path)) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          active: true,
          proxy_url: path.includes('/B/') ? '/mock-desktop/B' : '/mock-desktop/mock',
          viewer_held: true,
          computer_use_active: false,
        }),
      });
      return;
    }

    const startMatch = path.match(/\/workspaces\/([^/]+)\/desktop\/$/);
    if (method === 'POST' && startMatch) {
      if (startMatch[1] === 'A' && options.holdWorkspaceAStart) {
        await options.holdWorkspaceAStart;
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ task_id: `mock-start-${startMatch[1]}` }),
      });
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
}

test.describe('desktop viewer recovery regression', () => {
  test.beforeEach(({ browser }) => {
    expect(browser.browserType().name()).toBe(browserName);
  });

  test('keeps one live iframe through sidebar/modal visibility and retries a disconnect', async ({ page }) => {
    const apiCalls: ApiCall[] = [];
    await installDiagnosticPage(page, apiCalls);

    await page.getByTestId('open-side-panel').click();
    await page.evaluate(() => {
      (window as DiagnosticWindow).__desktopDiagnostic?.connectMock('A', '/mock-desktop/A');
    });
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

    await page.getByTestId('open-desktop-modal').click();
    await expect(page.getByTestId('workspace-desktop-modal')).toBeVisible();
    await assertSameViewer();
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

    // Keep a visual artifact for debugging a failed or changed regression.
    await page.screenshot({
      path: `${screenshotDir}/desktop-recovery-${browserName}.png`,
      fullPage: true,
    });
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
    await page.evaluate(() => {
      (window as DiagnosticWindow).__desktopDiagnostic?.connectMock('B', '/mock-desktop/B');
    });
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
    ).length).toBe(1);
    await page.waitForTimeout(150);

    expect(apiCalls.some((call) =>
      call.method === 'POST' && /\/workspaces\/A\/desktop\/$/.test(call.path),
    )).toBe(false);
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

  test('waits for an in-flight A start before stopping A and preserves B', async ({ page }) => {
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
    await page.evaluate(() => {
      (window as DiagnosticWindow).__desktopDiagnostic?.connectMock('B', '/mock-desktop/B');
    });
    await expect.poll(() => page.evaluate(() =>
      (window as DiagnosticWindow).__desktopDiagnostic?.snapshot(),
    )).toEqual({
      workspaceId: 'B',
      proxyUrl: '/mock-desktop/B',
      isConnected: true,
      isConnecting: false,
    });

    // A's start response is still blocked, so its stop must be queued rather
    // than racing ahead and allowing the delayed start to recreate its lease.
    expect(apiCalls.filter((call) =>
      call.method === 'POST' && /\/workspaces\/A\/desktop\/stop\/$/.test(call.path),
    )).toHaveLength(0);

    releaseAStart();
    await startAResponse;
    await expect.poll(() => apiCalls.filter((call) =>
      call.method === 'POST' && /\/workspaces\/A\/desktop\/stop\/$/.test(call.path),
    ).length).toBe(1);

    // Allow any erroneous duplicate teardown to surface, then verify A was
    // started/stopped once and the late A completion did not replace B.
    await page.waitForTimeout(200);
    expect(apiCalls.filter((call) =>
      call.method === 'POST' && /\/workspaces\/A\/desktop\/$/.test(call.path),
    )).toHaveLength(1);
    expect(apiCalls.filter((call) =>
      call.method === 'POST' && /\/workspaces\/A\/desktop\/stop\/$/.test(call.path),
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
});
