import { mkdirSync } from 'node:fs'
import { createServer, type Server } from 'node:http'
import { networkInterfaces } from 'node:os'
import { test, expect, type Page } from '@playwright/test'

const viteUrl = process.env.CLIPBOARD_VITE_URL || 'http://127.0.0.1:5173'
const origins = {
  insecure: 'http://192.0.2.1',
  secure: 'https://desktop-clipboard.invalid',
} as const
const workspaceId = 'desktop-clipboard-workspace'
const artifacts = '/workspace/.opencuria/playwright'
mkdirSync(artifacts, { recursive: true })

type BackendWrite = { text: string; startedAt: number; finishedAt?: number; status?: number }
type BackendRead = { text: string; startedAt: number; finishedAt: number; copySelection?: boolean; shift?: boolean }
type SelectionRequest = { copy_selection: boolean; shift: boolean; startedAt: number }
type Fixture = {
  writes: BackendWrite[]
  reads: BackendRead[]
  apiRequests: string[]
  selectionRequests: SelectionRequest[]
  selectionText: string
  selectionDelayMs: number
  selectionStatus: number
  selectionOrigin?: string
  closeSelectionServer?: () => Promise<void>
  writeStatus: number
  releaseWrite: (() => void) | null
}
type GuestEvent = { type: string; key: string; ctrlKey: boolean; metaKey: boolean; at: number }
type DebugWindow = Window & {
  __desktopClipboardFixture?: {
    setVmClipboard(text: string): void
    setComputerUse(active: boolean): void
    setGuestCopyDelay(ms: number): void
    bumpViewer(): void
  }
  __clipboardReadTextCalls?: number
}
const fixtures = new WeakMap<Page, Fixture>()

function makeFixture(writeStatus = 200): Fixture {
  return { writes: [], reads: [], apiRequests: [], selectionRequests: [], selectionText: '', selectionDelayMs: 0, selectionStatus: 200, writeStatus, releaseWrite: null }
}

/** Real TCP HTTP endpoint: sync XHR cannot use Playwright route.fulfill on Linux WebKit. */
async function startSelectionServer(fixture: Fixture): Promise<string> {
  const address = process.env.CLIPBOARD_SELECTION_HOST || Object.values(networkInterfaces()).flat().find((item) => item && item.family === 'IPv4' && !item.internal && !item.address.startsWith('169.254.'))?.address
  if (!address) throw new Error('No external IPv4 address is available for the real HTTP clipboard fixture')
  const server: Server = createServer((request, response) => {
    const path = request.url ?? '/'
    if (request.method === 'GET' && path.startsWith('/mock-kasm')) {
      response.writeHead(200, { 'Content-Type': 'text/html' }).end(mockKasmHtml)
      return
    }
    if (request.method === 'GET' && path === '/desktop-clipboard-diagnostic') {
      response.writeHead(200, { 'Content-Type': 'text/html' }).end(
        '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Desktop clipboard fixture</title></head><body><div id="desktop-diagnostic-app"></div><script type="module" src="/src/desktop-clipboard-diagnostic.js"></script></body></html>',
      )
      return
    }
    if (request.method === 'GET') {
      void fetch(`${viteUrl}${path}`).then(async (proxied) => {
        response.writeHead(proxied.status, Object.fromEntries(proxied.headers.entries()))
        response.end(Buffer.from(await proxied.arrayBuffer()))
      }).catch(() => response.writeHead(502).end('Vite proxy failed'))
      return
    }
    response.setHeader('Access-Control-Allow-Origin', '*')
    response.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS')
    response.setHeader('Access-Control-Allow-Headers', 'Content-Type')
    if (request.method === 'OPTIONS') { response.writeHead(204).end(); return }
    if (request.method !== 'POST' || path !== `/api/v1/workspaces/${workspaceId}/desktop/clipboard/read/`) {
      response.writeHead(404).end('not found'); return
    }
    let body = ''
    request.setEncoding('utf8')
    request.on('data', (chunk: string) => { body += chunk })
    request.on('end', () => {
      let parsed: { copy_selection?: unknown; shift?: unknown }
      try { parsed = JSON.parse(body) as typeof parsed } catch { response.writeHead(400).end('invalid JSON'); return }
      if (parsed.copy_selection !== true || typeof parsed.shift !== 'boolean') {
        response.writeHead(400, { 'Content-Type': 'application/json' }).end(JSON.stringify({ detail: 'copy_selection and shift required' })); return
      }
      const startedAt = Date.now()
      fixture.selectionRequests.push({ copy_selection: true, shift: parsed.shift, startedAt })
      fixture.apiRequests.push(`POST ${path}`)
      setTimeout(() => {
        const text = fixture.selectionText
        fixture.reads.push({ text, startedAt, finishedAt: Date.now(), copySelection: true, shift: parsed.shift as boolean })
        response.writeHead(fixture.selectionStatus, { 'Content-Type': 'application/json' })
          .end(JSON.stringify(fixture.selectionStatus >= 200 && fixture.selectionStatus < 300 ? { text } : { detail: 'fixture selection copy rejected' }))
      }, fixture.selectionDelayMs)
    })
  })
  await new Promise<void>((resolve, reject) => { server.once('error', reject); server.listen(0, address, resolve) })
  const origin = `http://${address}:${(server.address() as { port: number }).port}`
  fixture.selectionOrigin = origin
  fixture.closeSelectionServer = () => new Promise((resolve, reject) => {
    server.closeAllConnections(); server.close((error) => error ? reject(error) : resolve())
  })
  return origin
}

/** Mount the production surface, dialog, stores, and composable under Vite. */
async function installFixture(page: Page, origin: string, fixture: Fixture): Promise<void> {
  fixtures.set(page, fixture)
  const [surfaceResponse, dialogResponse, desktopStoreResponse] = await Promise.all([
    fetch(`${viteUrl}/src/components/workspaces/DesktopSurface.vue`),
    fetch(`${viteUrl}/src/components/common/ClipboardDialog.vue`),
    fetch(`${viteUrl}/src/stores/desktop.ts`),
  ])
  expect(surfaceResponse.ok, 'DesktopSurface served by Vite').toBeTruthy()
  expect(dialogResponse.ok, 'ClipboardDialog served by Vite').toBeTruthy()
  expect(desktopStoreResponse.ok, 'desktop store served by Vite').toBeTruthy()
  const allSource = (await Promise.all([
    surfaceResponse.text(), dialogResponse.text(), desktopStoreResponse.text(),
  ])).join('\n')
  const vue = allSource.match(/(["'])(\/node_modules\/\.vite\/deps\/vue\.js\?v=[^"']+)\1/)?.[2]
  const pinia = allSource.match(/(["'])(\/node_modules\/\.vite\/deps\/pinia\.js\?v=[^"']+)\1/)?.[2]
  expect(vue, 'optimized Vue import').toBeTruthy()
  expect(pinia, 'optimized Pinia import').toBeTruthy()

  // Proxy modules to Vite while preserving the test page's HTTP/HTTPS origin.
  if (!fixture.selectionOrigin) await page.route(`${origin}/**`, async (route) => {
    const url = new URL(route.request().url())
    await route.fulfill({ response: await route.fetch({ url: `${viteUrl}${url.pathname}${url.search}` }) })
  })
  // Use Vite's real client but remove HMR's socket bootstrap; no request to a
  // loopback/dev origin should change the isolated document's trust context.
  await page.route('**/@vite/client', async (route) => {
    const response = await route.fetch({ url: `${viteUrl}/@vite/client` })
    const body = await response.text()
    const noHmr = body.replace(/transport\.connect\(createHMRHandler\(handleMessage\)\);/, '')
    expect(noHmr).not.toBe(body)
    await route.fulfill({ response, body: noHmr })
  })
  if (!fixture.selectionOrigin) await page.route('**/mock-kasm*', (route) => route.fulfill({ contentType: 'text/html', body: mockKasmHtml }))
  await page.route('**/src/services/socket.ts*', (route) => route.fulfill({
    contentType: 'text/javascript',
    body: 'export const isConnected = { value: false }; export function onEvent() { return () => {}; }',
  }))
  await page.route('**/config.json', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ apiBaseUrl: fixture.selectionOrigin ? `${fixture.selectionOrigin}/api/v1` : '/api/v1', wsBaseUrl: '' }),
  }))
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    if (fixture.selectionOrigin === url.origin) return route.continue()
    fixture.apiRequests.push(`${route.request().method()} ${url.pathname}`)
    if (url.pathname === `/api/v1/workspaces/${workspaceId}/desktop/clipboard/read/`) {
      const startedAt = Date.now()
      const text = await page.evaluate(() =>
        document.querySelector<HTMLIFrameElement>('[data-testid="desktop-surface-iframe"]')
          ?.contentDocument?.documentElement.getAttribute('data-vm-clipboard') ?? '',
      )
      const read: BackendRead = { text, startedAt, finishedAt: Date.now() }
      fixture.reads.push(read)
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ text: read.text }) })
    }
    return route.fulfill({
      status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'Unexpected fixture API call' }),
    })
  })
  // This is an explicit response gate, not a timer approximation: each paste
  // test verifies nothing reaches Kasm until after it releases the >150ms write.
  await page.route(`**/api/v1/workspaces/${workspaceId}/desktop/clipboard/write/`, async (route) => {
    const body = route.request().postDataJSON() as { text?: unknown }
    const write: BackendWrite = {
      text: typeof body.text === 'string' ? body.text : '',
      startedAt: Date.now(),
    }
    fixture.writes.push(write)
    await new Promise<void>((resolve) => { fixture.releaseWrite = resolve })
    write.finishedAt = Date.now()
    write.status = fixture.writeStatus
    if (fixture.writeStatus >= 200 && fixture.writeStatus < 300) {
      // Only update the guest clipboard once the fixture server has accepted
      // the write. The surface remount test uses the old iframe's disconnected
      // event target to prove stale replay is discarded.
      await page.evaluate((text) => {
        document.querySelector<HTMLIFrameElement>('[data-testid="desktop-surface-iframe"]')
          ?.contentDocument?.documentElement.setAttribute('data-vm-clipboard', text)
      }, write.text)
      await route.fulfill({
        status: fixture.writeStatus,
        contentType: 'application/json',
        body: JSON.stringify({ text: write.text }),
      })
    } else {
      await route.fulfill({
        status: fixture.writeStatus,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'fixture write rejected' }),
      })
    }
  })
  await page.routeWebSocket(/desktop-clipboard\.invalid/, (socket) => socket.close())
  await page.routeWebSocket(/127\.0\.0\.1:5173/, (socket) => socket.close())

  const diagnosticModule = `
import { createApp, h } from ${JSON.stringify(vue)};
import { createPinia } from ${JSON.stringify(pinia)};
import '/src/assets/main.css';
import DesktopSurface from '/src/components/workspaces/DesktopSurface.vue';
import ClipboardDialog from '/src/components/common/ClipboardDialog.vue';
import { Toaster } from '/src/components/ui/sonner/index.ts';
import { sidebarDesktopHost } from '/src/lib/desktopSurfaceHost.ts';
import { useDesktopStore } from '/src/stores/desktop.ts';
import { useWorkspaceStore } from '/src/stores/workspaces.ts';
import { loadConfig } from '/src/services/config.ts';
await loadConfig();
const app = createApp({ setup() {
  const desktop = useDesktopStore();
  const workspaces = useWorkspaceStore();
  desktop.setConnected(${JSON.stringify(workspaceId)}, '/mock-kasm');
  workspaces.workspaces = [{ id: ${JSON.stringify(workspaceId)}, desktop_width: 1280, desktop_height: 720 }];
  window.__desktopClipboardFixture = {
    setVmClipboard(text) { document.querySelector('[data-testid="desktop-surface-iframe"]')?.contentDocument?.documentElement.setAttribute('data-vm-clipboard', text); },
    setComputerUse(active) { desktop.setComputerUseActive(active); },
    setGuestCopyDelay(ms) { document.querySelector('[data-testid="desktop-surface-iframe"]')?.contentWindow?.postMessage({ action: 'fixture-copy-delay', ms }, location.origin); },
    bumpViewer() { desktop.bumpViewer(); },
  };
  return () => h('main', { class: 'min-h-screen space-y-4 bg-background p-6 text-foreground' }, [
    h('header', { class: 'space-y-1' }, [
      h('h1', { class: 'text-xl font-semibold' }, 'Desktop clipboard browser regression'),
      h('p', { class: 'text-sm' }, 'Real DesktopSurface, ClipboardDialog, and native host clipboard gestures.'),
    ]),
    h('section', { class: 'grid gap-3 sm:grid-cols-2' }, [
      h('label', { class: 'grid gap-1 text-sm' }, ['Host clipboard source', h('textarea', {
        'data-testid': 'clipboard-source', 'aria-label': 'Host clipboard source', rows: 3,
        class: 'rounded border border-border bg-background p-2 font-mono text-xs',
      })]),
      h('label', { class: 'grid gap-1 text-sm' }, ['Host clipboard read-back', h('textarea', {
        'data-testid': 'clipboard-readback', 'aria-label': 'Host clipboard read-back', rows: 3,
        class: 'rounded border border-border bg-background p-2 font-mono text-xs',
      })]),
    ]),
    h('section', { id: 'desktop-host', class: 'h-[650px] w-full overflow-hidden rounded-lg border bg-black', 'data-testid': 'desktop-host' }),
    h(DesktopSurface, { workspaceId: ${JSON.stringify(workspaceId)} }),
    h(ClipboardDialog), h(Toaster),
  ]);
} });
app.use(createPinia());
app.mount('#desktop-diagnostic-app');
sidebarDesktopHost.value = document.querySelector('#desktop-host');
`
  if (!fixture.selectionOrigin) await page.route('**/desktop-clipboard-diagnostic', (route) => route.fulfill({
    contentType: 'text/html',
    body: '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Desktop clipboard fixture</title></head><body><div id="desktop-diagnostic-app"></div><script type="module" src="/src/desktop-clipboard-diagnostic.js"></script></body></html>',
  }))
  await page.route('**/src/desktop-clipboard-diagnostic.js', (route) => route.fulfill({
    contentType: 'text/javascript', body: diagnosticModule,
  }))

  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.stack ?? error.message))
  page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()) })
  await page.goto(`${origin}/desktop-clipboard-diagnostic`)
  await expect(page.getByTestId('desktop-surface-iframe'), `fixture errors: ${errors.join('\n')}`).toBeVisible()
  await expect(page.frameLocator('[data-testid="desktop-surface-iframe"]').locator('#desktop-canvas')).toBeVisible()
  expect(await page.evaluate(() => window.isSecureContext)).toBe(origin.startsWith('https:'))
  if (origin.startsWith('http:')) {
    if (!fixture.selectionOrigin)
      expect(await page.evaluate(() => typeof navigator.clipboard?.readText)).toBe('undefined')
    // Prove the desktop paste route consumes native ClipboardEvent data, rather
    // than sneaking in an asynchronous clipboard read on insecure HTTP.
    await page.evaluate(() => {
      ;(window as DebugWindow).__clipboardReadTextCalls = 0
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
        readText: async () => {
          ;(window as DebugWindow).__clipboardReadTextCalls! += 1
          throw new Error('Desktop paste must not call readText')
        },
      } })
    })
  }
  await expect(page.locator('[data-testid="desktop-surface-loading"]')).toHaveCount(0)
  expect(errors, 'production component mount errors').toEqual([])
}

// Kasm stand-in: trusted OS key events are stopped by the desktop clipboard
// helper, which then replays synthetic guest Ctrl+C/V. Kasm sends clipboardrx
// as an ordinary postMessage after guest copy (not as another trusted gesture).
const mockKasmHtml = `<!doctype html><html class="noVNC_connected" data-vm-clipboard="guest default clipboard"><head><meta charset="utf-8"><style>html,body{margin:0;width:100%;height:100%;background:#111}canvas{display:block;width:100%;height:100%;outline:none}</style></head><body><canvas id="desktop-canvas" tabindex="0" aria-label="Mock Kasm desktop canvas"></canvas><script>(()=>{
const root=document.documentElement,canvas=document.querySelector('#desktop-canvas');root.__guestEvents=[];let copyDelay=0;
window.addEventListener('message',event=>{if(event.source===parent&&event.origin===location.origin&&event.data?.action==='fixture-copy-delay')copyDelay=Math.max(0,Number(event.data.ms)||0)});
canvas.addEventListener('keydown',event=>{const key=event.key.toLowerCase();if(!(event.ctrlKey&&(key==='c'||key==='v')))return;event.preventDefault();event.stopPropagation();if(event.isTrusted)return;root.__guestEvents.push({type:'keydown',key,ctrlKey:event.ctrlKey,metaKey:event.metaKey,at:Date.now()});if(key==='c')setTimeout(()=>parent.postMessage({action:'clipboardrx',value:root.getAttribute('data-vm-clipboard')||''},location.origin),copyDelay);else{root.setAttribute('data-guest-pasted',root.getAttribute('data-vm-clipboard')||'');root.setAttribute('data-guest-v-at',String(Date.now()));}},true);canvas.focus();})();</script></body></html>`

async function primeClipboard(page: Page, text: string): Promise<void> {
  const source = page.getByTestId('clipboard-source')
  const readback = page.getByTestId('clipboard-readback')
  await source.fill(text)
  await source.focus()
  await source.selectText()
  await page.keyboard.press('Control+C')
  await readback.focus()
  await readback.fill('')
  await page.keyboard.press('Control+V')
  await expect(readback, 'native host clipboard round-trip').toHaveValue(text)
}
async function focusCanvas(page: Page): Promise<void> {
  await page.frameLocator('[data-testid="desktop-surface-iframe"]').locator('#desktop-canvas').focus()
}
async function guestEvents(page: Page): Promise<GuestEvent[]> {
  return page.frameLocator('[data-testid="desktop-surface-iframe"]').locator('html').evaluate((el) =>
    (el as HTMLElement & { __guestEvents?: GuestEvent[] }).__guestEvents ?? [],
  )
}
async function nativePasteHost(page: Page): Promise<string> {
  const readback = page.getByTestId('clipboard-readback')
  await readback.focus()
  await readback.fill('')
  await page.keyboard.press('Control+V')
  return readback.inputValue()
}
async function screenshot(page: Page, name: string): Promise<void> {
  await page.screenshot({ path: `${artifacts}/desktop-clipboard-${name}.png`, fullPage: true })
}
async function noDialog(page: Page): Promise<void> {
  await expect(page.getByRole('dialog', { name: /^Copy / })).toHaveCount(0)
}
async function noReadText(page: Page): Promise<void> {
  await expect.poll(() => page.evaluate(() => (window as DebugWindow).__clipboardReadTextCalls)).toBe(0)
}

test.describe('real DesktopSurface clipboard bridge', () => {
  test.afterEach(async ({ page }, info) => {
    // Release any test-held API response even if an assertion aborts the test.
    const fixture = fixtures.get(page)
    fixture?.releaseWrite?.()
    await fixture?.closeSelectionServer?.().catch(() => undefined)
    if (info.status !== info.expectedStatus) {
      const title = info.title.toLowerCase().replace(/[^a-z0-9]+/g, '-')
      await screenshot(page, `${info.project.name}-failure-${title}`).catch(() => undefined)
    }
  })

  test('insecure native paste forwards exact unicode, multiline and repeated large text after one delayed write', async ({ page }) => {
    const fixture = makeFixture()
    await installFixture(page, origins.insecure, fixture)
    const payloads = [
      'Grüße 👩🏽‍💻 — 東京\nsecond line\twith tabs',
      'repeat Unicode\n🌍 café\n'.repeat(128),
      'large λ/漢字/👾\n'.repeat(1024),
      'large λ/漢字/👾\n'.repeat(1024),
    ]
    for (const [index, text] of payloads.entries()) {
      const baseline = fixture.writes.length
      await primeClipboard(page, text)
      await focusCanvas(page)
      await page.keyboard.press('Control+V')
      await expect.poll(() => fixture.writes.length, { message: `write ${index + 1} reached backend` }).toBe(baseline + 1)
      expect(fixture.writes[baseline]?.text).toBe(text)
      await page.waitForTimeout(200)
      expect(fixture.writes[baseline]?.finishedAt).toBeUndefined()
      expect((await guestEvents(page)).filter((event) => event.type === 'keydown' && event.key === 'v')).toHaveLength(index)
      fixture.releaseWrite?.()
      fixture.releaseWrite = null
      await expect.poll(async () => (await guestEvents(page)).filter((event) => event.type === 'keydown' && event.key === 'v').length).toBe(index + 1)
      const guestV = (await guestEvents(page)).filter((event) => event.type === 'keydown' && event.key === 'v').at(-1)!
      const write = fixture.writes[baseline]!
      expect(write.finishedAt! - write.startedAt).toBeGreaterThanOrEqual(150)
      expect(guestV.at).toBeGreaterThanOrEqual(write.finishedAt!)
      expect(guestV).toMatchObject({ ctrlKey: true, metaKey: false })
      await expect(page.frameLocator('[data-testid="desktop-surface-iframe"]').locator('html')).toHaveAttribute('data-guest-pasted', text)
      expect(fixture.writes).toHaveLength(baseline + 1)
      await noReadText(page)
    }
    await noDialog(page)
    await screenshot(page, `${test.info().project.name}-insecure-native-paste`)
  })

  test('failed VM clipboard write never forwards guest Ctrl+V', async ({ page }) => {
    const fixture = makeFixture(503)
    await installFixture(page, origins.insecure, fixture)
    await primeClipboard(page, 'failure sentinel\nline two')
    await focusCanvas(page)
    await page.keyboard.press('Control+V')
    await expect.poll(() => fixture.writes.length).toBe(1)
    await page.waitForTimeout(200)
    expect((await guestEvents(page)).filter((event) => event.type === 'keydown' && event.key === 'v')).toHaveLength(0)
    fixture.releaseWrite?.()
    fixture.releaseWrite = null
    await page.waitForTimeout(100)
    expect((await guestEvents(page)).filter((event) => event.type === 'keydown' && event.key === 'v')).toHaveLength(0)
    expect(fixture.writes).toHaveLength(1)
    await noReadText(page)
    await noDialog(page)
    await screenshot(page, `${test.info().project.name}-failed-write`)
  })

  test('host editor shortcuts and computer-use overlay do not enter desktop bridge', async ({ page }) => {
    const fixture = makeFixture()
    await installFixture(page, origins.insecure, fixture)
    const localText = 'local editor only — do not send to VM'
    await primeClipboard(page, localText)
    const source = page.getByTestId('clipboard-source')
    await source.fill('')
    await source.focus()
    await page.keyboard.press('Control+V')
    await expect(source).toHaveValue(localText)
    await source.selectText()
    await page.keyboard.press('Control+C')
    expect(fixture.writes).toHaveLength(0)
    expect(await guestEvents(page)).toHaveLength(0)

    // Keep the Kasm canvas active: the overlay may block input visually, but
    // the bridge must also reject shortcuts while the embedded viewer stays focused.
    await focusCanvas(page)
    await expect(page.frameLocator('[data-testid="desktop-surface-iframe"]').locator('#desktop-canvas')).toBeFocused()
    await page.evaluate(() => (window as DebugWindow).__desktopClipboardFixture?.setComputerUse(true))
    await expect(page.getByText(/Computer-use is controlling this desktop/)).toBeVisible()
    await expect(page.frameLocator('[data-testid="desktop-surface-iframe"]').locator('#desktop-canvas')).toBeFocused()
    await page.keyboard.press('Control+C')
    await page.keyboard.press('Control+V')
    await page.waitForTimeout(300)
    expect(fixture.writes).toHaveLength(0)
    expect(fixture.reads).toHaveLength(0)
    expect(fixture.apiRequests).toEqual([])
    expect(await guestEvents(page)).toHaveLength(0)
    await noDialog(page)
    await screenshot(page, `${test.info().project.name}-computer-use-focused-iframe`)
  })

  test('secure deferred guest copy performs real iframe-realm writes for new and repeated payloads', async ({ page }) => {
    if (test.info().project.name === 'chromium') {
      await page.context().grantPermissions(['clipboard-read', 'clipboard-write'], { origin: origins.secure })
    }
    const fixture = makeFixture()
    await installFixture(page, origins.secure, fixture)
    await primeClipboard(page, 'secure sentinel')
    const child = page.frameLocator('[data-testid="desktop-surface-iframe"]').locator('html')
    await child.evaluate((html) => {
      const win = html.ownerDocument.defaultView as Window & {
        __clipboardCalls?: Array<{ childRealm: boolean; itemFromChildRealm: boolean }>
      }
      const clipboard = win.navigator.clipboard
      const nativeWrite = clipboard.write.bind(clipboard)
      win.__clipboardCalls = []
      Object.defineProperty(clipboard, 'write', {
        configurable: true,
        value: function (items: ClipboardItems) {
          win.__clipboardCalls!.push({ childRealm: this === clipboard, itemFromChildRealm: items[0] instanceof (win as Window & typeof globalThis).ClipboardItem })
          return nativeWrite(items)
        },
      })
    })

    const payloads = [
      'remote clipboard: first 😀\nsecond line',
      'new secure clipboard content — 東京',
      'new secure clipboard content — 東京',
    ]
    const readback = page.getByTestId('clipboard-readback')
    for (const [index, text] of payloads.entries()) {
      await page.evaluate((value) => (window as DebugWindow).__desktopClipboardFixture?.setVmClipboard(value), text)
      await focusCanvas(page)
      await page.keyboard.press('Control+C')
      await expect.poll(() => child.evaluate((html) =>
        (html.ownerDocument.defaultView as Window & { __clipboardCalls?: unknown[] }).__clipboardCalls?.length ?? 0,
      )).toBe(index + 1)
      expect(await child.evaluate((html) =>
        (html.ownerDocument.defaultView as Window & { __clipboardCalls?: unknown[] }).__clipboardCalls,
      )).toEqual(Array.from({ length: index + 1 }, () => ({ childRealm: true, itemFromChildRealm: true })))
      await noDialog(page)
      await readback.focus()
      await readback.fill('')
      await page.keyboard.press('Control+V')
      await expect(readback, `native paste of secure copy ${index + 1}`).toHaveValue(text)
      const guestCopy = (await guestEvents(page)).filter((event) => event.type === 'keydown' && event.key === 'c').at(-1)
      expect(guestCopy).toMatchObject({ ctrlKey: true, metaKey: false })
    }
    expect(fixture.writes).toHaveLength(0)
    await noDialog(page)
    await screenshot(page, `${test.info().project.name}-secure-deferred-copy`)
  })

  test('blocked secure clipboard.write preserves native sentinel and opens no copy dialog', async ({ page }) => {
    if (test.info().project.name === 'chromium') {
      await page.context().grantPermissions(['clipboard-read', 'clipboard-write'], { origin: origins.secure })
    }
    const fixture = makeFixture()
    await installFixture(page, origins.secure, fixture)
    const sentinel = 'blocked API must preserve native sentinel'
    await primeClipboard(page, sentinel)
    const child = page.frameLocator('[data-testid="desktop-surface-iframe"]').locator('html')
    await child.evaluate((html) => {
      const win = html.ownerDocument.defaultView as Window & { __blockedClipboardWriteCalls?: number }
      win.__blockedClipboardWriteCalls = 0
      Object.defineProperty(win.navigator, 'clipboard', {
        configurable: true,
        value: { write: async () => {
          win.__blockedClipboardWriteCalls! += 1
          throw new Error('fixture denied clipboard write')
        } },
      })
      html.ownerDocument.execCommand = () => false
    })
    await page.evaluate(() => (window as DebugWindow).__desktopClipboardFixture?.setVmClipboard('blocked remote payload'))
    await focusCanvas(page)
    await page.keyboard.press('Control+C')
    await expect.poll(() => child.evaluate((html) =>
      (html.ownerDocument.defaultView as Window & { __blockedClipboardWriteCalls?: number }).__blockedClipboardWriteCalls ?? 0,
    )).toBe(1)
    await noDialog(page)
    const readback = page.getByTestId('clipboard-readback')
    await readback.focus()
    await readback.fill('')
    await page.keyboard.press('Control+V')
    await expect(readback).toHaveValue(sentinel)
    expect(fixture.writes).toHaveLength(0)
    await screenshot(page, `${test.info().project.name}-blocked-write-preserves-sentinel`)
  })

  test('paste response arriving after iframe remount never replays into the stale guest', async ({ page }) => {
    const fixture = makeFixture()
    await installFixture(page, origins.insecure, fixture)
    await primeClipboard(page, 'stale request paste payload')
    await focusCanvas(page)
    const oldFrameHandle = await page.getByTestId('desktop-surface-iframe').elementHandle()
    expect(oldFrameHandle).not.toBeNull()
    await page.keyboard.press('Control+V')
    await expect.poll(() => fixture.writes.length).toBe(1)
    await page.evaluate(() => (window as DebugWindow).__desktopClipboardFixture?.bumpViewer())
    await expect.poll(() => oldFrameHandle!.evaluate((frame) => frame.isConnected)).toBe(false)
    const replacement = await page.getByTestId('desktop-surface-iframe').elementHandle()
    expect(replacement).not.toBeNull()
    await expect(page.frameLocator('[data-testid="desktop-surface-iframe"]').locator('#desktop-canvas')).toBeVisible()
    fixture.releaseWrite?.()
    fixture.releaseWrite = null
    await page.waitForTimeout(300)
    await expect(page.frameLocator('[data-testid="desktop-surface-iframe"]').locator('html')).not.toHaveAttribute('data-guest-pasted', /.+/)
    expect(fixture.writes).toHaveLength(1)
    // The replacement guest must receive no replay; target.isConnected in the
    // production helper prevents delivery into the detached old canvas.
    await noDialog(page)
    await screenshot(page, `${test.info().project.name}-stale-remount`)
  })

  test('synthetic Meta+C is separately mapped to guest Ctrl+C', async ({ page }) => {
    const fixture = makeFixture()
    await installFixture(page, origins.secure, fixture)
    await focusCanvas(page)
    await page.evaluate(() => {
      const frame = document.querySelector<HTMLIFrameElement>('[data-testid="desktop-surface-iframe"]')!
      const guestWindow = frame.contentWindow!
      guestWindow.dispatchEvent(new (guestWindow as Window & typeof globalThis).KeyboardEvent('keydown', { key: 'c', metaKey: true, bubbles: true, cancelable: true }))
      guestWindow.dispatchEvent(new (guestWindow as Window & typeof globalThis).KeyboardEvent('keyup', { key: 'c', metaKey: true, bubbles: true, cancelable: true }))
    })
    await expect.poll(async () => (await guestEvents(page)).filter((event) => event.type === 'keydown' && event.key === 'c').length).toBe(1)
    expect((await guestEvents(page)).find((event) => event.type === 'keydown' && event.key === 'c')).toMatchObject({ ctrlKey: true, metaKey: false })
    await noDialog(page)
    await screenshot(page, `${test.info().project.name}-synthetic-meta-to-guest-ctrl`)
  })

  test('synthetic Meta+V with ClipboardEvent text waits for backend then replays guest Ctrl+V exactly once', async ({ page }) => {
    const fixture = makeFixture()
    await installFixture(page, origins.insecure, fixture)
    const text = 'synthetic macOS chord payload — λ\nsecond line'
    await focusCanvas(page)

    // This is deliberately synthetic, not a real macOS keyboard gesture. It
    // exercises Meta normalization and native ClipboardEvent data separately.
    await page.evaluate((payload) => {
      const frame = document.querySelector<HTMLIFrameElement>('[data-testid="desktop-surface-iframe"]')!
      const guestWindow = frame.contentWindow as (Window & typeof globalThis)
      guestWindow.dispatchEvent(new guestWindow.KeyboardEvent('keydown', {
        key: 'v', code: 'KeyV', metaKey: true, bubbles: true, cancelable: true,
      }))
      const transfer = new guestWindow.DataTransfer()
      transfer.setData('text/plain', payload)
      const paste = new guestWindow.ClipboardEvent('paste', {
        bubbles: true, cancelable: true, clipboardData: transfer,
      })
      guestWindow.dispatchEvent(paste)
      guestWindow.dispatchEvent(new guestWindow.KeyboardEvent('keyup', {
        key: 'v', code: 'KeyV', metaKey: true, bubbles: true, cancelable: true,
      }))
    }, text)

    await expect.poll(() => fixture.writes.length).toBe(1)
    expect(fixture.writes[0]?.text).toBe(text)
    await page.waitForTimeout(200)
    expect((await guestEvents(page)).filter((event) => event.type === 'keydown' && event.key === 'v')).toHaveLength(0)
    fixture.releaseWrite?.()
    fixture.releaseWrite = null
    await expect.poll(async () => (await guestEvents(page)).filter((event) => event.type === 'keydown' && event.key === 'v').length).toBe(1)
    const guestV = (await guestEvents(page)).find((event) => event.type === 'keydown' && event.key === 'v')!
    expect(guestV).toMatchObject({ ctrlKey: true, metaKey: false })
    expect(fixture.writes).toHaveLength(1)
    await expect(page.frameLocator('[data-testid="desktop-surface-iframe"]').locator('html')).toHaveAttribute('data-guest-pasted', text)
    await noDialog(page)
    await screenshot(page, `${test.info().project.name}-synthetic-meta-via-clipboard-event`)
  })

  test('delayed secure guest clipboardrx uses REST fallback and copies exact text on both engines', async ({ page }) => {
    if (test.info().project.name === 'chromium') {
      await page.context().grantPermissions(['clipboard-read', 'clipboard-write'], { origin: origins.secure })
    }
    const fixture = makeFixture()
    await installFixture(page, origins.secure, fixture)
    const text = 'secure delayed REST fallback — 😀\nclipboard payload'
    await page.evaluate((payload) => {
      const debug = window as DebugWindow
      debug.__desktopClipboardFixture?.setVmClipboard(payload)
      debug.__desktopClipboardFixture?.setGuestCopyDelay(600)
    }, text)
    await focusCanvas(page)
    await page.keyboard.press('Control+C')

    // The mock Kasm postMessage arrives after the production reader deadline;
    // this exercises the secure deferred ClipboardItem path on WebKit without
    // granting unsupported Playwright permissions there.
    await expect.poll(() => fixture.reads.length, { timeout: 8_000 }).toBe(1)
    expect(fixture.reads[0]?.text).toBe(text)
    const guestCopy = (await guestEvents(page)).find((event) => event.type === 'keydown' && event.key === 'c')
    expect(guestCopy).toBeDefined()
    expect(fixture.reads[0]!.startedAt - guestCopy!.at).toBeGreaterThanOrEqual(300)
    const readback = page.getByTestId('clipboard-readback')
    await readback.focus()
    await readback.fill('')
    await page.keyboard.press('Control+V')
    await expect(readback, 'secure delayed REST fallback copied by native paste').toHaveValue(text)
    expect(fixture.apiRequests.filter((request) => request.includes('/desktop/clipboard/read/'))).toHaveLength(1)
    expect(fixture.writes).toHaveLength(0)
    await noDialog(page)
    await screenshot(page, `${test.info().project.name}-delayed-secure-clipboardrx-rest-fallback`)
  })

  test('HTTP trusted copy gesture sends selection RPC and copies fresh exact text once', async ({ page }) => {
    const fixture = makeFixture()
    const origin = await startSelectionServer(fixture)
    await installFixture(page, origin, fixture)
    await primeClipboard(page, 'native clipboard sentinel before HTTP selection copy')

    const repeated = 'same fresh selection on repeated copy — café 東京 😀\nsecond line'
    const large = 'λ/漢字/👾\n'.repeat(13_000)
    expect(large.length).toBeGreaterThan(100_000)
    const cases = [
      { text: 'new selection, not the old host clipboard — Grüße 👩🏽‍💻\nsecond line', shift: false, delay: 250 },
      { text: repeated, shift: true, delay: 600 },
      { text: repeated, shift: false, delay: 250 },
      { text: large, shift: false, delay: 600 },
    ]
    const readback = page.getByTestId('clipboard-readback')
    for (const [index, item] of cases.entries()) {
      fixture.selectionText = item.text
      fixture.selectionDelayMs = item.delay
      await focusCanvas(page)
      await page.keyboard.press(item.shift ? 'Control+Shift+C' : 'Control+C')
      await expect.poll(() => fixture.selectionRequests.length, { message: `api=${JSON.stringify(fixture.apiRequests)}` }).toBe(index + 1)
      await expect.poll(() => fixture.reads.length).toBe(index + 1)
      expect(fixture.selectionRequests[index]).toMatchObject({ copy_selection: true, shift: item.shift })
      const read = fixture.reads[index]!
      expect(read).toMatchObject({ text: item.text, copySelection: true, shift: item.shift })
      expect(read.finishedAt - read.startedAt).toBeGreaterThanOrEqual(item.delay - 10)
      expect(fixture.apiRequests.filter((request) => request.includes('/desktop/clipboard/read/'))).toHaveLength(index + 1)
      await noDialog(page)
      await expect(page.getByText('Copy failed')).toHaveCount(0)
      await noReadText(page)
      // Runner sends guest C atomically; no synthetic iframe C may be replayed.
      expect(await guestEvents(page), 'no duplicate iframe C in atomic HTTP path').toEqual([])
      await readback.focus()
      await readback.fill('')
      await page.keyboard.press('Control+V')
      await expect(readback, `native paste of HTTP copy ${index + 1}`).toHaveValue(item.text)
    }
    expect(fixture.writes).toHaveLength(0)
    await noDialog(page)
    await screenshot(page, `${test.info().project.name}-http-gesture-selection-copy`)
  })

  test('failed HTTP selection copy preserves native sentinel, reports an actionable error, and opens no dialog', async ({ page }) => {
    const fixture = makeFixture()
    fixture.selectionStatus = 503
    fixture.selectionText = 'must not replace native clipboard sentinel'
    fixture.selectionDelayMs = 250
    const origin = await startSelectionServer(fixture)
    await installFixture(page, origin, fixture)
    const sentinel = 'preserve native host clipboard on HTTP selection failure'
    await primeClipboard(page, sentinel)
    await focusCanvas(page)
    await page.keyboard.press('Control+C')
    await expect.poll(() => fixture.selectionRequests.length).toBe(1)
    expect(fixture.selectionRequests[0]).toMatchObject({ copy_selection: true, shift: false })
    await expect(page.getByText('Unable to copy the desktop selection. Try again.')).toBeVisible()
    await noDialog(page)
    expect(await guestEvents(page), 'failed atomic request must not replay iframe C').toEqual([])
    const readback = page.getByTestId('clipboard-readback')
    await readback.focus()
    await readback.fill('')
    await page.keyboard.press('Control+V')
    await expect(readback, 'failed selection copy preserves native sentinel').toHaveValue(sentinel)
    expect(fixture.selectionRequests).toHaveLength(1)
    expect(fixture.writes).toHaveLength(0)
    await noDialog(page)
    await screenshot(page, `${test.info().project.name}-http-selection-copy-failed`)
  })
})
