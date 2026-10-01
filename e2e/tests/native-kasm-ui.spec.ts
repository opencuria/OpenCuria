import { mkdir } from 'node:fs/promises'
import { test, expect } from '@playwright/test'

const baseUrl = process.env.E2E_BASE_URL ?? 'http://127.0.0.1:5173'
const fixtureUrl = process.env.NATIVE_HTTP_URL ?? 'http://127.0.0.1:8100/host.html'
const fixtureOrigin = new URL(fixtureUrl).origin

declare global {
  interface Window {
    __nativeUi?: { connect: () => void; open: () => void; openModal: () => void }
  }
}
const workspaceId = 'native-ui-test'

test.skip(process.env.NATIVE_KASM_CLIPBOARD_E2E !== '1', 'requires isolated native Xvnc fixture')
test.setTimeout(120_000)

const socketStub = `
const handlers = new Map();
export function onEvent(name, callback) { const values=handlers.get(name)||new Set(); values.add(callback); handlers.set(name,values); return () => values.delete(callback); }
export function emitTestEvent(name, data) { for (const callback of handlers.get(name)||[]) callback(data); }
`

const diagnostic = `<!doctype html><html><head><meta charset="utf-8"><title>Native desktop UI diagnostic</title><link rel="stylesheet" href="/src/assets/main.css"><style>
body{margin:0}[data-testid="diagnostic-sidebar"]{height:calc(100vh - 48px);margin-top:48px}[data-testid="side-panel-desktop"]{display:flex;flex-direction:column;height:100%;min-height:0}[data-testid="side-panel-desktop"]>div:last-child{position:relative;height:560px;min-height:0}[data-testid="side-panel-desktop-host"]{position:absolute;inset:0;min-height:560px}[data-testid="desktop-surface"]>div:first-child{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;overflow:hidden}[data-testid="desktop-surface"] iframe{display:block}
</style></head><body><div id="app"></div><script type="module" src="/src/native-ui-diagnostic.js"></script></body></html>`

async function installActualDesktopUI(page: import('@playwright/test').Page, apiCalls: string[]): Promise<void> {
  const surface = await fetch(`${baseUrl}/src/components/workspaces/DesktopSurface.vue`).then(response => response.text())
  const store = await fetch(`${baseUrl}/src/stores/desktop.ts`).then(response => response.text())
  const vueUrl = surface.match(/(["'])(\/node_modules\/\.vite\/deps\/vue\.js\?v=[^"']+)\1/)?.[2]
  const piniaUrl = store.match(/(["'])(\/node_modules\/\.vite\/deps\/pinia\.js\?v=[^"']+)\1/)?.[2]
  expect(vueUrl).toBeTruthy()
  expect(piniaUrl).toBeTruthy()
  const appModule = `
import '/src/assets/main.css';
import { createApp, h } from ${JSON.stringify(vueUrl)};
import { createPinia } from ${JSON.stringify(piniaUrl)};
import DesktopSurface from '/src/components/workspaces/DesktopSurface.vue';
import SidePanelDesktop from '/src/components/workspaces/SidePanelDesktop.vue';
import WorkspaceDesktop from '/src/components/workspaces/WorkspaceDesktop.vue';
import { useDesktopStore } from '/src/stores/desktop.ts';
import { useSidePanelStore } from '/src/stores/sidePanel.ts';
const app = createApp({ setup() {
 const desktop=useDesktopStore(); const panel=useSidePanelStore();
 window.__nativeUi = { connect: () => { desktop.setConnected(${JSON.stringify(workspaceId)}, '/mock-desktop/vnc.html'); desktop.setComputerUseActive(false); requestAnimationFrame(() => requestAnimationFrame(() => { const frame=document.querySelector('[data-testid=desktop-surface-iframe]'); const url=new URL(frame.src); url.searchParams.set('host','127.0.0.1'); url.searchParams.set('port','${process.env.NATIVE_WS_PORT}'); url.searchParams.set('path','websockify'); url.searchParams.set('encrypt','false'); url.searchParams.set('autoconnect','true'); frame.src=url.toString(); })); }, open: () => panel.open('desktop'), openModal: () => desktop.open() };
 return () => h('main',{style:{display:'flex',minHeight:'100vh',position:'relative'}},[
  h('nav',{style:{position:'fixed',top:'0',left:'0',zIndex:'100'}},[h('button',{onClick:()=>panel.open('desktop')},'Open sidebar'),h('button',{onClick:()=>desktop.open()},'Open desktop modal')]),
  h('section',{style:{display:'block',width:panel.isOpen?'620px':'0px',height:'calc(100vh - 48px)',marginTop:'48px',visibility:panel.isOpen?'visible':'hidden'}},panel.hasOpened?[h(SidePanelDesktop,{key:${JSON.stringify(workspaceId)},workspaceId:${JSON.stringify(workspaceId)}})]:[]),
  h(WorkspaceDesktop,{workspaceId:${JSON.stringify(workspaceId)}}),h(DesktopSurface,{workspaceId:${JSON.stringify(workspaceId)}})
 ]);
}}); app.use(createPinia()); app.mount('#app');
`
  await page.route('**/native-ui', route => route.fulfill({ status: 200, contentType: 'text/html', body: diagnostic }))
  await page.route('**/src/native-ui-diagnostic.js', route => route.fulfill({ status: 200, contentType: 'text/javascript', body: appModule }))
  await page.route('**/src/services/socket.ts*', route => route.fulfill({ status: 200, contentType: 'text/javascript', body: socketStub }))
  await page.route('**/config.json', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ apiBaseUrl: '/api/v1', wsBaseUrl: '' }) }))
  await page.route('**/api/**', async route => {
    const request = route.request()
    const url = new URL(request.url())
    apiCalls.push(`${request.method()} ${url.pathname}`)
    if (request.method() === 'GET' && url.pathname.endsWith('/desktop/status/')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ active: false, proxy_url: null, viewer_held: false, computer_use_active: false }) })
      return
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ task_id: 'native-fixture-task' }) })
  })
  await page.route('**/mock-desktop/**', async route => {
    const url = new URL(route.request().url())
    const assetPath = url.pathname.replace(/^\/mock-desktop\//, '')
    const target = assetPath === 'vnc.html' ? '/vnc.html' : `/${assetPath}`
    const response = await fetch(`${fixtureOrigin}${target}`)
    if (!response.ok) return route.fulfill({ status: response.status, body: await response.text() })
    let body: Buffer | string = Buffer.from(await response.arrayBuffer())
    const contentType = response.headers.get('content-type') ?? 'application/octet-stream'
    if (assetPath === 'vnc.html') {
      let html = body.toString()
      const settings = `<base href="${fixtureOrigin}/">`
      html = html.replace('<title>KasmVNC</title>', `<title>KasmVNC</title>${settings}`)
      body = html
    } else if (assetPath === 'dist/main.bundle.js') {
      const { execFileSync } = await import('node:child_process')
      body = execFileSync('/workspace/OpenCuria/backend/.venv/bin/python', ['-c', `import os,sys;sys.path.insert(0,'/workspace/OpenCuria/backend');os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings');import django;django.setup();from apps.runners.desktop_proxy import apply_vnc_client_patches;sys.stdout.buffer.write(apply_vnc_client_patches('/dist/main.bundle.js',[],sys.stdin.buffer.read())[1])`], { input: Buffer.from(body) })
    }
    await route.fulfill({ status: 200, contentType, body })
  })
  await page.goto(`${baseUrl}/native-ui`)
}

test('actual DesktopSurface fallback button opens the native panel; textarea writes to real X11 clipboard', async ({ page, context }) => {
  const apiCalls: string[] = []
  const pageErrors: string[] = []
  page.on('pageerror', error => pageErrors.push(error.message))
  await mkdir('/workspace/.opencuria/playwright', { recursive: true })
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: fixtureOrigin })
  await installActualDesktopUI(page, apiCalls)
  await page.getByRole('button', { name: 'Open sidebar' }).click()
  await expect.poll(() => apiCalls.some(call => call.includes('/desktop/status/'))).toBe(true)
  await expect.poll(() => apiCalls.some(call => call.includes('POST') && call.endsWith('/desktop/'))).toBe(true)
  await page.evaluate(() => window.__nativeUi?.connect())
  const frame = page.frameLocator('[data-testid="desktop-surface-iframe"]')
  await expect(frame.locator('html')).toHaveClass(/noVNC_connected/, { timeout: 30_000 })
  await expect(page.getByTestId('desktop-surface')).toBeVisible()

  const cdp = await context.newCDPSession(page)
  await cdp.send('Browser.setPermission', {
    permission: { name: 'clipboard-read' },
    setting: 'denied',
    origin: fixtureOrigin,
  })
  await frame.locator('#noVNC_container canvas').click({ position: { x: 200, y: 180 } })
  await expect(page.getByTestId('desktop-clipboard-fallback')).toContainText('Clipboard access was denied', { timeout: 15_000 })
  await page.getByRole('button', { name: 'Open KasmVNC clipboard' }).click()
  await expect(frame.locator('#opencuria-clipboard-panel')).toBeVisible()
  await expect(frame.locator('#opencuria-clipboard-panel')).toHaveCSS('background-color', 'rgb(32, 36, 42)')
  await expect(page.locator('body')).toHaveCSS('font-family', /Noto Sans/)
  const textarea = frame.locator('#noVNC_clipboard_text')
  await textarea.fill('manual clipboard from actual dashboard UI')
  await textarea.dispatchEvent('change')

  const { spawn } = await import('node:child_process')
  await expect.poll(() => new Promise<string>((resolve) => {
    const child = spawn('xclip', ['-selection', 'clipboard', '-out', '-target', 'UTF8_STRING'], {
      env: { ...process.env, DISPLAY: process.env.NATIVE_X_DISPLAY }, stdio: ['ignore', 'pipe', 'ignore'],
    })
    let text = ''
    child.stdout.on('data', chunk => { text += String(chunk) })
    child.on('close', code => resolve(code === 0 ? text : ''))
    setTimeout(() => child.kill('SIGKILL'), 1_000)
  }), { timeout: 10_000 }).toBe('manual clipboard from actual dashboard UI')

  await expect(page.locator('[title="Copy VM clipboard to local clipboard"]')).toHaveCount(0)
  await expect(page.locator('[title="Paste local clipboard into VM clipboard"]')).toHaveCount(0)
  expect(pageErrors, 'unexpected page errors').toEqual([])
  await page.screenshot({ path: '/workspace/.opencuria/playwright/native-kasm-dashboard.png', fullPage: true })
})
