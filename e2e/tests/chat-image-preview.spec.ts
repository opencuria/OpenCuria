import { mkdirSync } from 'node:fs'
import { test, expect, type Page, type Locator } from '@playwright/test'

const BASE_URL = process.env.E2E_BASE_URL || 'http://127.0.0.1:5173'
const artifacts = '/workspace/.opencuria/playwright'
mkdirSync(artifacts, { recursive: true })
test.use({
  baseURL: BASE_URL,
  browserName: 'chromium',
  headless: true,
  launchOptions: { executablePath: '/usr/bin/google-chrome' },
  viewport: { width: 1280, height: 900 },
})

const svg =
  '<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="900"><rect width="1600" height="900" fill="#93c5fd"/><circle cx="1250" cy="180" r="90" fill="#fde68a"/><path d="M0 800L450 200L900 800L1250 430L1600 800V900H0Z" fill="#166534"/><text x="80" y="120" font-size="64" fill="white">1600 × 900 regression photo</text></svg>'
const dataUrl = `data:image/svg+xml;base64,${Buffer.from(svg).toString('base64')}`
const remoteUrl = 'https://chat-image.invalid/photo.svg'

async function installDiagnostic(page: Page): Promise<void> {
  // Resolve the same optimized runtime imports used by actual Vite components.
  const [component, store] = await Promise.all([
    fetch(`${BASE_URL}/src/components/chat/HarnessMarkdown.vue`),
    fetch(`${BASE_URL}/src/stores/workspaceImages.ts`),
  ])
  expect(component.ok).toBeTruthy()
  expect(store.ok).toBeTruthy()
  const vue = (await component.text()).match(
    /(["'])(\/node_modules\/\.vite\/deps\/vue\.js\?v=[^"']+)\1/,
  )?.[2]
  const pinia = (await store.text()).match(
    /(["'])(\/node_modules\/\.vite\/deps\/pinia\.js\?v=[^"']+)\1/,
  )?.[2]
  expect(vue, 'optimized Vue import').toBeTruthy()
  expect(pinia, 'optimized Pinia import').toBeTruthy()
  const module = `
import { createApp, h, ref, provide } from ${JSON.stringify(vue)};
import { createPinia } from ${JSON.stringify(pinia)};
import '/src/assets/main.css';
import HarnessMessageView from '/src/components/chat/HarnessMessageView.vue';
import HarnessMarkdown from '/src/components/chat/HarnessMarkdown.vue';
import ToolDetailRead from '/src/components/chat/tools/ToolDetailRead.vue';
import ToolDetailDefault from '/src/components/chat/tools/ToolDetailDefault.vue';
import { harnessWorkspaceIdKey } from '/src/lib/harnessWorkspaceContext.ts';
import { useWorkspaceImageStore } from '/src/stores/workspaceImages.ts';
// MessageView renders real HarnessMentionImages, and both paths use the real
// ImageLightbox with its shadcn Dialog portal. Nothing is component-stubbed.
const app = createApp({ setup() {
  provide(harnessWorkspaceIdKey, ref('image-diagnostic'));
  const images = useWorkspaceImageStore();
  images.imageCache['/workspace/photo.svg'] = ${JSON.stringify(dataUrl)};
  images.fetchingPaths['/workspace/loading.svg'] = true;
  images.fetchingPaths['/workspace/missing.svg'] = true;
  window.__showMissing = () => { delete images.fetchingPaths['/workspace/missing.svg']; };
  const section = (id, child) => h('section', { 'data-testid': id, class: 'mb-6' }, [child]);
  const text = '![Workspace photo](/workspace/photo.svg)';
  return () => h('main', { class: 'mx-auto w-full max-w-2xl p-4' }, [
    h('button', { 'data-testid': 'outside-focus' }, 'Outside focus target'),
    section('workspace-photo', h(HarnessMessageView, { models: [], message: {
      id: 'assistant-photo', session_id: 'diagnostic', role: 'assistant', content: text,
      parts: [{ id: 'photo-text', session_id: 'diagnostic', type: 'text', state: 'completed', title: '', output: text }],
    } })),
    section('sent-user', h(HarnessMessageView, { models: [], message: {
      id: 'user-photo', session_id: 'diagnostic', role: 'user',
      content: 'Inspect @file:/workspace/photo.svg', parts: [],
    } })),
    section('remote-photo', h(HarnessMarkdown, { text: ${JSON.stringify(`![Remote photo](${remoteUrl})`)} })),
    section('linked-photo', h(HarnessMarkdown, { text: ${JSON.stringify(`[![Linked photo](${remoteUrl})](${BASE_URL}/linked-destination)`)} })),
    ...[ToolDetailRead, ToolDetailDefault].map((component, index) => section('tool-photo-' + index,
      h(component, { part: { id: 'tool-' + index, session_id: 'diagnostic', type: 'tool',
        state: 'completed', tool: index === 0 ? 'read' : 'screenshot', output: 'Image read successfully',
        meta: { attachments: [{ type: 'file', mime: 'image/svg+xml', url: ${JSON.stringify(dataUrl)}, filename: 'photo.svg' }] },
      } }))),
    section('broken-photo', h(HarnessMarkdown, { text: '![Broken photo](https://chat-image.invalid/broken.png)' })),
    section('loading-photo', h(HarnessMarkdown, { text: '![Loading photo](/workspace/loading.svg)' })),
    section('missing-photo', h(HarnessMarkdown, { text: '![Missing photo](/workspace/missing.svg)' })),
  ]);
} });
app.use(createPinia()); app.mount('#diagnostic-app');
`
  await page.route('**/chat-image-diagnostic', (route) =>
    route.fulfill({
      contentType: 'text/html',
      body: '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Chat image diagnostic</title></head><body><div id="diagnostic-app"></div><script type="module" src="/src/chat-image-diagnostic.js"></script></body></html>',
    }),
  )
  await page.route('**/src/chat-image-diagnostic.js', (route) =>
    route.fulfill({ contentType: 'text/javascript', body: module }),
  )
  await page
    .context()
    .route('https://chat-image.invalid/broken.png', (route) =>
      route.fulfill({ status: 404, body: 'Missing image' }),
    )
  await page
    .context()
    .route(remoteUrl, (route) => route.fulfill({ contentType: 'image/svg+xml', body: svg }))
  await page.context().route('**/linked-destination', (route) =>
    route.fulfill({
      contentType: 'text/html',
      body: '<!doctype html><title>Linked destination</title><h1>Linked destination</h1>',
    }),
  )
  const backend: string[] = []
  await page.route('**/api/**', (route) => {
    backend.push(route.request().url())
    return route.abort()
  })
  await page.routeWebSocket(/\/(ws|socket\.io)\//, (socket) => socket.close())
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.goto('/chat-image-diagnostic')
  await expect(trigger(page)).toBeVisible()
  await expect(page.getByTestId('sent-user').getByTestId('mention-image-button')).toBeVisible()
  await expect
    .poll(() =>
      page
        .getByTestId('remote-photo')
        .locator('img')
        .evaluate((el) => (el as HTMLImageElement).naturalWidth),
    )
    .toBe(1600)
  expect(errors, 'component mount errors').toEqual([])
  expect(backend, 'no providers or backend required').toEqual([])
}

const trigger = (page: Page) =>
  page.getByTestId('workspace-photo').getByRole('button', {
    name: 'Open image preview Workspace photo',
    exact: true,
  })
const viewer = (page: Page) => page.getByRole('dialog', { name: 'Image Viewer', exact: true })
const shot = (page: Page, name: string) =>
  page.screenshot({ path: `${artifacts}/chat-image-preview-${name}.png` })
async function openViewer(page: Page): Promise<Locator> {
  const dialog = viewer(page)
  await expect(dialog).toBeVisible()
  await expect(dialog).toHaveAttribute('aria-modal', 'true')
  await expect
    .poll(() =>
      dialog.locator('img').evaluate((el) => ({
        width: (el as HTMLImageElement).naturalWidth,
        height: (el as HTMLImageElement).naturalHeight,
      })),
    )
    .toEqual({ width: 1600, height: 900 })
  await expect(dialog.getByText('100%', { exact: true })).toBeVisible()
  await expect(dialog.getByRole('button', { name: 'Zoom out', exact: true })).toBeDisabled()
  return dialog
}
async function transform(image: Locator) {
  // Inline transform is the target state, independent of animation timing.
  return image.evaluate((el) => {
    const m = new DOMMatrix((el as HTMLElement).style.transform)
    return { scale: m.a, x: m.m41, y: m.m42 }
  })
}
async function escape(page: Page, opener: Locator) {
  await page.keyboard.press('Escape')
  await expect(viewer(page)).toHaveCount(0)
  await expect(opener).toBeFocused()
}

test.describe('actual Vue chat image preview regression', () => {
  test.afterEach(async ({ page }, testInfo) => {
    if (testInfo.status !== testInfo.expectedStatus) {
      const name = testInfo.title.toLowerCase().replace(/[^a-z0-9]+/g, '-')
      await shot(page, `failure-${name}`)
    }
  })
  // The page fixture uses a fresh, isolated browser context for every test.
  test('workspace photo expands, zoom controls/wheel/doubleclick, reset, pan and Escape', async ({
    page,
  }) => {
    await installDiagnostic(page)
    const opener = trigger(page)
    const inline = await opener.locator('img').boundingBox()
    expect(inline).not.toBeNull()
    await opener.click()
    const dialog = await openViewer(page)
    const image = dialog.locator('img')
    await expect
      .poll(async () => (await image.boundingBox())?.width ?? 0)
      .toBeGreaterThan(inline!.width)
    expect((await image.boundingBox())!.height).toBeGreaterThan(inline!.height)
    await shot(page, 'desktop')
    await dialog.getByRole('button', { name: 'Zoom in', exact: true }).click()
    await expect(dialog.getByText('120%', { exact: true })).toBeVisible()
    await dialog.getByRole('button', { name: 'Zoom out', exact: true }).click()
    await expect(dialog.getByText('100%', { exact: true })).toBeVisible()
    await page.keyboard.press('+')
    await expect(dialog.getByText('120%', { exact: true })).toBeVisible()
    await dialog.getByRole('button', { name: 'Reset view', exact: true }).click()
    await expect.poll(() => transform(image)).toEqual({ scale: 1, x: 0, y: 0 })
    await image.hover()
    await page.mouse.wheel(0, -100)
    await expect(dialog.getByText('120%', { exact: true })).toBeVisible()
    await page.mouse.wheel(0, 100)
    await expect(dialog.getByText('100%', { exact: true })).toBeVisible()
    await image.dblclick()
    await expect(dialog.getByText('120%', { exact: true })).toBeVisible()
    await image.click() // A captured image click must not close the viewer.
    await expect(dialog).toBeVisible()
    for (let i = 0; i < 2; i++)
      await dialog.getByRole('button', { name: 'Zoom in', exact: true }).click()
    await expect.poll(async () => (await transform(image)).scale).toBeGreaterThan(1.5)
    const box = await image.boundingBox()
    const before = await transform(image)
    await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2)
    await page.mouse.down()
    await page.mouse.move(box!.x + box!.width / 2 + 65, box!.y + box!.height / 2 + 45, { steps: 8 })
    const after = await transform(image)
    expect(after.x).toBeGreaterThan(before.x)
    expect(after.y).toBeGreaterThan(before.y)
    await page.mouse.up()
    await expect(dialog).toBeVisible()
    await shot(page, 'zoom-pan')
    await image.dblclick()
    await expect.poll(() => transform(image)).toEqual({ scale: 1, x: 0, y: 0 })
    await escape(page, opener)
    await opener.click()
    await openViewer(page)
    await page.mouse.click(3, 100) // Empty stage space still dismisses the preview.
    await expect(viewer(page)).toHaveCount(0)
    await expect(opener).toBeFocused()
  })

  test('keyboard Enter/Space activation and focus trapping', async ({ page }) => {
    await installDiagnostic(page)
    const opener = trigger(page)
    for (const key of ['Enter', 'Space']) {
      await opener.focus()
      await page.keyboard.press(key)
      const dialog = await openViewer(page)
      const first = dialog.getByRole('button', {
        name: 'Zoom in',
        exact: true,
      })
      const last = dialog.getByRole('button', {
        name: 'Close image preview',
        exact: true,
      })
      await last.focus()
      await page.keyboard.press('Tab')
      await expect(first).toBeFocused()
      await page.keyboard.press('Shift+Tab')
      await expect(last).toBeFocused()
      for (let i = 0; i < 8; i++) {
        await page.keyboard.press('Tab')
        expect(await dialog.evaluate((el) => el.contains(document.activeElement))).toBe(true)
      }
      await escape(page, opener)
    }
    await shot(page, 'keyboard-closed')
  })

  test('sent-user mention, remote images, linked navigation and loading/missing tiles', async ({
    page,
    context,
  }) => {
    await installDiagnostic(page)
    const mention = page.getByTestId('sent-user').getByTestId('mention-image-button')
    await expect(page.getByTestId('sent-user').getByTestId('mention-image-remove')).toHaveCount(0)
    await mention.click()
    const dialog = await openViewer(page)
    await expect(dialog.locator('img')).toHaveAttribute('alt', 'photo.svg')
    await shot(page, 'user-mention')
    await dialog.getByRole('button', { name: 'Close image preview', exact: true }).click()
    await expect(viewer(page)).toHaveCount(0)
    await expect(mention).toBeFocused()
    const remote = page.getByTestId('remote-photo').getByRole('button', {
      name: 'Open image preview Remote photo',
      exact: true,
    })
    await remote.click()
    await openViewer(page)
    await expect(viewer(page).locator('img')).toHaveAttribute('src', remoteUrl)
    await escape(page, remote)
    for (const key of ['Enter', 'Space']) {
      await remote.focus()
      await page.keyboard.press(key)
      await openViewer(page)
      await escape(page, remote)
    }
    const loading = page.getByTestId('loading-photo')
    const missing = page.getByTestId('missing-photo')
    await expect(loading.getByTestId('harness-markdown-media-loading')).toBeVisible()
    await page.evaluate(() => (window as Window & { __showMissing?: () => void }).__showMissing?.())
    await expect(missing.getByTestId('harness-markdown-media-fallback')).toHaveText('Missing photo')
    for (const tile of [loading, missing]) {
      await expect(tile.getByRole('button')).toHaveCount(0)
      await expect(tile.locator('[tabindex], [aria-haspopup="dialog"]')).toHaveCount(0)
      await tile.click()
      await expect(viewer(page)).toHaveCount(0)
    }
    const linked = page.getByTestId('linked-photo')
    await expect(linked.getByRole('button')).toHaveCount(0)
    await expect(linked.locator('img')).not.toHaveAttribute('tabindex', '0')
    const link = linked.getByRole('link')
    await expect(link).toHaveAttribute('href', `${BASE_URL}/linked-destination`)
    const popupPromise = context.waitForEvent('page')
    await link.click()
    const popup = await popupPromise
    await expect(popup).toHaveURL(`${BASE_URL}/linked-destination`)
    await expect(popup.getByRole('heading', { name: 'Linked destination' })).toBeVisible()
    await expect(viewer(page)).toHaveCount(0)
    await popup.close()
    await shot(page, 'unavailable')
  })

  test('tool image attachments reuse the viewer and failed images remain dismissible', async ({
    page,
  }) => {
    await installDiagnostic(page)
    for (const index of [0, 1]) {
      const opener = page.getByTestId(`tool-photo-${index}`).getByRole('button')
      await opener.click()
      await openViewer(page)
      await escape(page, opener)
    }
    const broken = page.getByTestId('broken-photo').getByRole('button')
    await broken.click()
    const dialog = viewer(page)
    await expect(dialog.getByRole('status')).toHaveText('Image could not be loaded.')
    await expect(dialog.getByRole('button', { name: 'Zoom in', exact: true })).toBeDisabled()
    await expect(dialog.getByRole('button', { name: 'Reset view', exact: true })).toBeDisabled()
    await dialog.getByRole('button', { name: 'Close image preview', exact: true }).click()
    await expect(viewer(page)).toHaveCount(0)
    await expect(broken).toBeFocused()
  })

  test('mobile 390 × 844 fits image and closes accessibly', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await installDiagnostic(page)
    await trigger(page).click()
    const dialog = await openViewer(page)
    const image = dialog.locator('img')
    await expect.poll(async () => (await image.boundingBox())?.width ?? 0).toBeGreaterThan(300)
    const box = (await image.boundingBox())!
    expect(box.x).toBeGreaterThanOrEqual(0)
    expect(box.y).toBeGreaterThanOrEqual(0)
    expect(box.x + box.width).toBeLessThanOrEqual(390)
    expect(box.y + box.height).toBeLessThanOrEqual(844)
    expect(box.width / box.height).toBeCloseTo(1600 / 900, 2)
    const close = dialog.getByRole('button', {
      name: 'Close image preview',
      exact: true,
    })
    await expect(close).toBeInViewport()
    await shot(page, 'mobile')
    await close.click()
    await expect(viewer(page)).toHaveCount(0)
    await expect(trigger(page)).toBeFocused()
  })
})
