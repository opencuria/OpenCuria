import { spawn, type ChildProcess } from 'node:child_process'
import { mkdir, readFile } from 'node:fs/promises'
import path from 'node:path'
import { test, expect, type Page, type FrameLocator } from '@playwright/test'

const artifactRoot = '/workspace/.opencuria/playwright'
const fixtureUrl = process.env.NATIVE_HTTP_URL ?? 'http://127.0.0.1:8100/host.html'
const fixtureOrigin = new URL(fixtureUrl).origin
const display = process.env.NATIVE_X_DISPLAY
// Four deliberately distinct pixels make a byte-level PNG assertion fail if
// Chromium/Kasm changes colors, channel order, or only transfers a blank image.
const png = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAACXBIWXMAAAAAAAAAAQCEeRdzAAAAEklEQVR4nGP4z8DwHwyBNBgAAEnICff5q7YNAAAAAElFTkSuQmCC',
  'base64',
)
const expectedRgba = Buffer.from([
  255, 0, 0, 255, 0, 255, 0, 255,
  0, 0, 255, 255, 255, 255, 255, 255,
])

test.skip(process.env.NATIVE_KASM_CLIPBOARD_E2E !== '1', 'requires the isolated Xvnc fixture')
test.setTimeout(120_000)

function xclipRead(mime: string, timeout = 1_500): Promise<Buffer | null> {
  return new Promise((resolve, reject) => {
    const child = spawn('xclip', ['-selection', 'clipboard', '-out', '-target', mime], {
      env: { ...process.env, DISPLAY: display },
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    const stdout: Buffer[] = []
    const stderr: Buffer[] = []
    const timer = setTimeout(() => child.kill('SIGKILL'), timeout)
    child.stdout.on('data', chunk => stdout.push(Buffer.from(chunk)))
    child.stderr.on('data', chunk => stderr.push(Buffer.from(chunk)))
    child.on('error', error => { clearTimeout(timer); reject(error) })
    child.on('close', code => {
      clearTimeout(timer)
      if (code === 0) resolve(Buffer.concat(stdout))
      else if (code === null || code === 1 || code === 137) resolve(null)
      else reject(new Error(`xclip read ${mime} exited ${code}: ${Buffer.concat(stderr)}`))
    })
  })
}

function startXclipOwner(mime: string, content: Buffer | string): ChildProcess {
  const child = spawn('xclip', ['-quiet', '-in', '-selection', 'clipboard', '-target', mime], {
    env: { ...process.env, DISPLAY: display },
    stdio: ['pipe', 'ignore', 'pipe'],
  })
  child.stdin!.end(content)
  return child
}

async function waitForVmClipboard(mime: string, content: Buffer | string): Promise<Buffer> {
  const expected = Buffer.isBuffer(content) ? content : Buffer.from(content)
  let actual: Buffer | null = null
  await expect.poll(async () => {
    actual = await xclipRead(mime)
    return actual
  }, { timeout: 10_000 }).toEqual(expected)
  return actual!
}

async function focusDesktop(page: Page, clickCanvas = true): Promise<void> {
  await page.evaluate(() => {
    const input = document.querySelector<HTMLTextAreaElement>('#parent-user-input')!
    input.focus()
    input.blur()
    document.querySelector<HTMLIFrameElement>('#desktop')?.focus()
  })
  if (clickCanvas) await page.frameLocator('#desktop').locator('#noVNC_container canvas')
    .click({ position: { x: 220, y: 200 } })
}

async function clipboardRead(frame: FrameLocator, mime: string): Promise<string | Buffer> {
  const result = await frame.locator('body').evaluate(async (_body, requestedMime) => {
    const items = await navigator.clipboard.read()
    for (const item of items) {
      if (!item.types.includes(requestedMime)) continue
      const blob = await item.getType(requestedMime)
      if (requestedMime === 'image/png') return Array.from(new Uint8Array(await blob.arrayBuffer()))
      return blob.text()
    }
    throw new Error(`Missing clipboard MIME ${requestedMime}; got ${items.flatMap(item => item.types)}`)
  }, mime)
  return Array.isArray(result) ? Buffer.from(result) : result
}

async function writeClipboard(page: Page, mime: string, value: string | Buffer): Promise<void> {
  const bytes = Buffer.isBuffer(value) ? Array.from(value) : null
  await page.evaluate(async ({ type, text, binary }) => {
    const blob = binary ? new Blob([new Uint8Array(binary)], { type }) : new Blob([text], { type })
    await navigator.clipboard.write([new ClipboardItem({ [type]: blob })])
  }, { type: mime, text: Buffer.isBuffer(value) ? '' : value, binary: bytes })
}

async function decodePng(pngBytes: Buffer): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const decoder = spawn('ffmpeg', [
      '-v', 'error', '-i', 'pipe:0', '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgba', 'pipe:1',
    ], { stdio: ['pipe', 'pipe', 'pipe'] })
    const rgba: Buffer[] = []
    const stderr: Buffer[] = []
    decoder.stdout.on('data', chunk => rgba.push(Buffer.from(chunk)))
    decoder.stderr.on('data', chunk => stderr.push(Buffer.from(chunk)))
    decoder.on('error', reject)
    decoder.on('close', code => {
      if (code === 0) resolve(Buffer.concat(rgba))
      else reject(new Error(`PNG decode failed (${code}): ${Buffer.concat(stderr)}`))
    })
    decoder.stdin.end(pngBytes)
  })
}

test('pinned KasmVNC clipboard transfers text, HTML and PNG both ways on real Xvnc', async ({ page, context }) => {
  test.skip(!display, 'the fixture must supply its isolated Xvnc DISPLAY')
  await mkdir(artifactRoot, { recursive: true })
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], {
    origin: fixtureOrigin,
  })
  await page.goto(fixtureUrl)
  const frame = page.frameLocator('#desktop')
  await expect(frame.locator('html')).toHaveClass(/noVNC_connected/, { timeout: 30_000 })
  await expect.poll(() => frame.locator('#noVNC_container canvas').isVisible()).toBe(true)

  // Browser → Xvnc: actual native Clipboard API writes initiate Kasm's real
  // binary clipboard protocol. Check target contents from the X11 selection.
  for (const [mime, value, readTarget] of [
    ['text/plain', 'OpenCuria VM text ✓\nline two 東京', 'UTF8_STRING'],
    ['text/html', '<p><strong>OpenCuria HTML</strong></p>', 'text/html'],
    ['image/png', png, 'image/png'],
  ] as const) {
    await writeClipboard(page, mime, value)
    await focusDesktop(page)
    await expect.poll(() => frame.locator('#noVNC_container canvas').evaluate((canvas) =>
      document.activeElement === canvas || canvas.contains(document.activeElement),
    )).toBe(true)
    if (mime === 'image/png') {
      let decoded: Buffer | null = null
      await expect.poll(async () => {
        const vmPng = await xclipRead(readTarget)
        if (!vmPng) return null
        decoded = await decodePng(vmPng)
        return decoded.toString('hex')
      }, { timeout: 10_000 }).toBe(expectedRgba.toString('hex'))
      expect(decoded, 'Browser → VM PNG decoded pixels').toEqual(expectedRgba)
    } else {
      await waitForVmClipboard(readTarget, value)
    }
  }

  // Let prior browser→VM writes settle. Xvnc retains the real VM selection
  // independently of the xclip owner, so focusing the iframe and waiting for
  // its one-shot trusted native read to settle cannot replace the VM contents.
  await page.waitForTimeout(250)
  await focusDesktop(page, false)
  await expect.poll(() => frame.locator('html').evaluate(html =>
    html.classList.contains('noVNC_connected') && document.hasFocus() &&
    document.visibilityState === 'visible' && document.activeElement?.tagName !== 'TEXTAREA',
  )).toBe(true)
  await page.waitForTimeout(500)

  // Xvnc → browser: each stable xclip owner advertises a real MIME target; Kasm
  // requests it over X11, forwards the server clipboard protocol, and Chromium
  // receives it through navigator.clipboard.read(), without test-side mocks.
  for (const [mime, ownerTarget, value] of [
    ['text/plain', 'UTF8_STRING', 'VM text → Chromium ✓\nmultiline'],
    ['text/html', 'text/html', '<html><body><p><b>VM HTML payload</b></p></body></html>'],
    ['image/png', 'image/png', png],
  ] as const) {
    const owner = startXclipOwner(ownerTarget, value)
    try {
      expect(owner.pid, `xclip owner did not start for ${mime}`).toBeTruthy()
      await expect.poll(async () => xclipRead(ownerTarget), { timeout: 10_000 })
        .toEqual(Buffer.isBuffer(value) ? value : Buffer.from(value))
      let received: string | Buffer | null = null
      const poll = expect.poll(async () => {
        try {
          received = await clipboardRead(frame, mime)
          return {
            error: null,
            value: mime === 'image/png'
              ? (await decodePng(received as unknown as Buffer)).toString('hex')
              : received,
          }
        } catch (error) {
          return { error: String(error), value: null }
        }
      }, { timeout: 15_000 })
      if (mime === 'text/plain') await poll.toEqual({ error: null, value })
      if (mime === 'text/html') await poll.toEqual({
        error: null, value: expect.stringContaining('VM HTML payload'),
      })
      if (mime === 'image/png') await poll.toEqual({ error: null, value: expectedRgba.toString('hex') })
      if (mime === 'image/png') {
        expect(Buffer.isBuffer(received), 'VM → browser PNG is binary').toBe(true)
        expect(await decodePng(received as unknown as Buffer), 'VM → browser PNG decoded pixels').toEqual(expectedRgba)
      }
      if (mime === 'text/html') expect(received).toContain('VM HTML payload')
    } finally {
      owner.kill('SIGTERM')
      await new Promise<void>(resolve => owner.once('close', () => resolve()))
    }
  }

  // Verify real trusted keyboard shortcuts against a native Tk text editor.
  // Ctrl+C/Ctrl+V cross X11, Kasm's keyboard events, its clipboard protocol,
  // and Chromium's clipboard; this deliberately uses no bridge/API mocks.
  const vmTextFile = process.env.NATIVE_VM_TEXT_FILE
  if (!vmTextFile) throw new Error('Fixture must provide NATIVE_VM_TEXT_FILE')
  const vmSeed = 'Remote clipboard seed — 東京\nSecond line from the VM'
  await expect.poll(() => readFile(vmTextFile, 'utf8')).toBe(vmSeed)
  const canvas = frame.locator('#noVNC_container canvas')
  const canvasSize = await canvas.boundingBox()
  expect(canvasSize).toBeTruthy()
  const scaleX = canvasSize!.width / 1024
  const scaleY = canvasSize!.height / 768
  const editorPoint = { x: 220 * scaleX, y: 150 * scaleY }
  await canvas.click({ position: editorPoint })
  await expect.poll(() => canvas.evaluate(element =>
    element === document.activeElement || element.contains(document.activeElement),
  )).toBe(true)
  const nativePasteText = 'Local keyboard paste from Chromium\nsecond line from browser'
  await writeClipboard(page, 'text/plain', nativePasteText)
  await expect.poll(() => clipboardRead(frame, 'text/plain'), { timeout: 10_000 })
    .toBe(nativePasteText)
  await canvas.click({ position: editorPoint })
  await expect.poll(() => canvas.evaluate(element =>
    element === document.activeElement || element.contains(document.activeElement),
  )).toBe(true)
  await canvas.press('Control+V')
  await expect.poll(() => readFile(vmTextFile, 'utf8'), { timeout: 10_000 })
    .toContain(nativePasteText)
  const { execFileSync } = await import('node:child_process')
  execFileSync('xdotool', [
    'search', '--sync', '--onlyvisible', '--name', 'OpenCuria native clipboard keyboard fixture',
    'windowfocus', '--sync', 'key', '--clearmodifiers', 'ctrl+c',
  ], { env: { ...process.env, DISPLAY: display } })
  await expect.poll(() => readFile(`${path.dirname(vmTextFile)}/native-text.copy-event`, 'utf8'))
    .toContain(nativePasteText)
  // Verify Ctrl+C actually updated the native X11 clipboard, then read that
  // same native selection through Kasm/Chromium before any further interaction.
  await expect.poll(async () => {
    const nativeClipboard = await xclipRead('UTF8_STRING')
    return nativeClipboard?.toString('utf8') ?? ''
  }, { timeout: 10_000 }).toContain(nativePasteText)
  await expect.poll(() => clipboardRead(frame, 'text/plain'), { timeout: 10_000 })
    .toContain(nativePasteText)

  // KasmVNC's native fallback panel remains a functional text route.
  await page.evaluate(() => (window as Window & { requestNativeClipboardPanel?: () => void })
    .requestNativeClipboardPanel?.())
  const fallback = frame.locator('#opencuria-clipboard-panel')
  await expect(fallback).toBeVisible()
  const textarea = frame.locator('#noVNC_clipboard_text')
  await textarea.fill('manual KasmVNC fallback text')
  await textarea.dispatchEvent('change')
  await waitForVmClipboard('UTF8_STRING', 'manual KasmVNC fallback text')

  // Computer-use pause gates both directions: browser clipboard writes must
  // not upload, and an actual VM owner update must not overwrite Chromium.
  await page.evaluate(() => (window as Window & { setTestContext?: (value: object) => void })
    .setTestContext?.({ computerUseActive: true }))
  await expect.poll(() => frame.locator('#opencuria-clipboard-panel').isVisible()).toBe(false)
  const browserBeforePause = 'browser clipboard remains authoritative during computer-use'
  await writeClipboard(page, 'text/plain', browserBeforePause)
  await focusDesktop(page)
  await frame.locator('#noVNC_container canvas').press('Control+V')
  expect(await xclipRead('UTF8_STRING')).toEqual(Buffer.from('manual KasmVNC fallback text'))

  const pausedVmText = 'VM clipboard is gated during computer-use'
  const pausedOwner = startXclipOwner('UTF8_STRING', pausedVmText)
  try {
    expect(await xclipRead('UTF8_STRING')).toEqual(Buffer.from(pausedVmText))
    await expect.poll(async () => {
      try { return await clipboardRead(frame, 'text/plain') } catch { return null }
    }, { timeout: 2_000 }).toBe(browserBeforePause)
    await new Promise(resolve => setTimeout(resolve, 300))
    expect(await clipboardRead(frame, 'text/plain')).toBe(browserBeforePause)
    expect(await xclipRead('UTF8_STRING')).toEqual(Buffer.from(pausedVmText))
  } finally {
    pausedOwner.kill('SIGTERM')
    await new Promise<void>(resolve => pausedOwner.once('close', () => resolve()))
  }

  // Resume explicitly: the native bridge must re-arm Kasm's focus resend when
  // the parent context transitions from gated to active.
  await page.evaluate(() => (window as Window & { setTestContext?: (value: object) => void })
    .setTestContext?.({ computerUseActive: false }))
  await focusDesktop(page)
  await expect(frame.locator('html')).toHaveClass(/noVNC_connected/)
  await page.screenshot({ path: path.join(artifactRoot, 'native-kasm-clipboard.png'), fullPage: false })
})
