import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'

import ImageLightbox from './ImageLightbox.vue'
import { voiceCaptureControls } from '@/lib/voiceCaptureControls'
import { activePointingWorkspace, pointingEngaged, pointingError } from '@/lib/voicePointing'

const notifyError = vi.hoisted(() => vi.fn())
vi.mock('@/stores/notifications', () => ({
  useNotificationStore: () => ({ error: notifyError }),
}))

let wrapper: VueWrapper
let stageRect: DOMRect
let stageTransformScale: number
let resizeStage: () => void
const disconnect = vi.fn()
const createObjectURL = vi.fn(() => 'blob:download')
const revokeObjectURL = vi.fn()
const OriginalURL = URL

async function loadImage(dialog: HTMLElement, width = 2000, height = 1400): Promise<void> {
  const image = dialog.querySelector('img')!
  Object.defineProperties(image, {
    naturalWidth: { value: width, configurable: true },
    naturalHeight: { value: height, configurable: true },
  })
  image.dispatchEvent(new Event('load'))
  await nextTick()
}

async function mountLightbox(load = true): Promise<HTMLElement> {
  wrapper = mount(ImageLightbox, {
    props: { src: 'data:image/png;base64,abc', alt: 'Chat photo' },
    attachTo: document.body,
  })
  await nextTick()
  await nextTick()
  const dialog = document.body.querySelector<HTMLElement>('[role="dialog"]')!
  const captured = new Set<number>()
  const stage = viewport(dialog)
  stage.setPointerCapture = vi.fn((id) => captured.add(id))
  stage.hasPointerCapture = vi.fn((id) => captured.has(id))
  stage.releasePointerCapture = vi.fn((id) => captured.delete(id))
  if (load) await loadImage(dialog)
  return dialog
}

function viewport(dialog: HTMLElement): HTMLElement {
  return dialog.querySelector<HTMLElement>('[data-slot="image-viewport"]')!
}

function button(name: string): HTMLButtonElement {
  return document.body.querySelector<HTMLButtonElement>(`button[aria-label="${name}"]`)!
}

function percent(dialog: HTMLElement): string {
  return dialog.querySelector('[aria-label="Zoom level"]')!.textContent!.trim()
}

function view(dialog: HTMLElement): { x: number; y: number; scale: number } {
  const transform = dialog.querySelector('img')!.style.transform
  const [x, y] = transform
    .match(/translate3d\(([^)]+)\)/)![1]!
    .split(',')
    .map(parseFloat)
  return { x: x!, y: y!, scale: Number(transform.match(/scale\(([^)]+)\)/)![1]) }
}

async function key(
  dialog: HTMLElement,
  value: string,
  options: KeyboardEventInit = {},
): Promise<void> {
  dialog.dispatchEvent(new KeyboardEvent('keydown', { key: value, bubbles: true, ...options }))
  await nextTick()
}

async function wheel(
  dialog: HTMLElement,
  deltaY: number,
  options: WheelEventInit = {},
): Promise<void> {
  viewport(dialog).dispatchEvent(
    new WheelEvent('wheel', {
      clientX: 770,
      clientY: 605,
      deltaY,
      bubbles: true,
      cancelable: true,
      ...options,
    }),
  )
  await nextTick()
}

async function pointer(
  dialog: HTMLElement,
  type: string,
  id: number,
  x: number,
  y: number,
  button = 0,
): Promise<void> {
  // jsdom does not implement pointer capture; the mounted viewport tracks it above.
  const event = new MouseEvent(type, {
    clientX: x + stageRect.left,
    clientY: y + stageRect.top,
    button,
    bubbles: true,
    cancelable: true,
  })
  Object.defineProperty(event, 'pointerId', { value: id })
  viewport(dialog).dispatchEvent(event)
  await nextTick()
}

async function doubleClick(dialog: HTMLElement, x = 500, y = 350): Promise<void> {
  dialog.querySelector('img')!.dispatchEvent(
    new MouseEvent('dblclick', {
      clientX: x + stageRect.left,
      clientY: y + stageRect.top,
      bubbles: true,
      cancelable: true,
    }),
  )
  await nextTick()
}

describe('ImageLightbox', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    stageRect = new DOMRect(20, 80, 1000, 700)
    stageTransformScale = 1
    vi.spyOn(Element.prototype, 'clientWidth', 'get').mockImplementation(function (this: Element) {
      return this.getAttribute('data-slot') === 'image-viewport' ? stageRect.width : 0
    })
    vi.spyOn(Element.prototype, 'clientHeight', 'get').mockImplementation(function (this: Element) {
      return this.getAttribute('data-slot') === 'image-viewport' ? stageRect.height : 0
    })
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (
      this: HTMLElement,
    ) {
      return this.dataset.slot === 'image-viewport'
        ? new DOMRect(
            stageRect.x,
            stageRect.y,
            stageRect.width * stageTransformScale,
            stageRect.height * stageTransformScale,
          )
        : new DOMRect()
    })
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(private callback: ResizeObserverCallback) {}
        observe = (element: HTMLElement) => {
          if (element.dataset.slot === 'image-viewport') {
            resizeStage = () => this.callback([], this as unknown as ResizeObserver)
          }
        }
        disconnect = disconnect
      },
    )
    vi.stubGlobal(
      'URL',
      class extends OriginalURL {
        static createObjectURL = createObjectURL
        static revokeObjectURL = revokeObjectURL
      },
    )
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        blob: async () => new Blob(['image bytes'], { type: 'image/png' }),
      })),
    )
  })

  afterEach(() => {
    wrapper?.unmount()
    voiceCaptureControls.value = null
    activePointingWorkspace.value = null
    pointingEngaged.value = false
    pointingError.value = null
    document.body.innerHTML = ''
    vi.useRealTimers()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('renders an accessible modal with only zoom, download, and close controls', async () => {
    const dialog = await mountLightbox()

    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(document.getElementById(dialog.getAttribute('aria-labelledby')!)?.textContent).toBe(
      'Image Viewer',
    )
    expect(dialog.querySelector('img')?.getAttribute('alt')).toBe('Chat photo')
    expect(dialog.querySelector('img')?.getAttribute('src')).toBe('data:image/png;base64,abc')
    expect(
      [...dialog.querySelectorAll('button')].map((el) => el.getAttribute('aria-label')),
    ).toEqual(['Zoom out', 'Zoom in', 'Download image', 'Close image preview'])
    expect(button('Zoom out').disabled).toBe(true)
    expect(button('Zoom in').disabled).toBe(false)
    expect(percent(dialog)).toBe('50%')
    expect(dialog.textContent).not.toContain('Chat photo')
    expect(dialog.textContent).not.toContain('Pointing')
  })

  it('preserves the original workspace image reference without adding idle dictation controls', async () => {
    const dialog = await mountLightbox()
    await wrapper.setProps({ path: '/workspace/design.png', workspaceId: 'workspace-a' })
    expect(dialog.querySelector('img')?.getAttribute('data-voice-image-path')).toBe(
      '/workspace/design.png',
    )
    expect(dialog.querySelector('img')?.getAttribute('data-voice-workspace')).toBe('workspace-a')
    expect(dialog.querySelectorAll('button')).toHaveLength(4)
  })

  it('shows controls for an active dictation and hides them when it ends', async () => {
    const dialog = await mountLightbox()
    expect(dialog.querySelector('[data-testid="voice-capture-bar"]')).toBeNull()
    const controls = {
      workspaceId: 'workspace-a',
      state: 'recording' as const,
      levels: [0.2, 0.5],
      stop: vi.fn(),
      send: vi.fn(),
      cancel: vi.fn(),
    }
    voiceCaptureControls.value = controls
    pointingEngaged.value = true
    await nextTick()
    expect(dialog.querySelector('[data-testid="voice-capture-bar"]')).not.toBeNull()
    expect(dialog.querySelector('[aria-label="Pointing is recorded"]')).not.toBeNull()
    button('Stop and edit').click()
    button('Stop and send').click()
    button('Discard recording').click()
    expect(controls.stop).toHaveBeenCalledOnce()
    expect(controls.send).toHaveBeenCalledOnce()
    expect(controls.cancel).toHaveBeenCalledOnce()
    voiceCaptureControls.value = null
    await nextTick()
    expect(dialog.querySelector('[data-testid="voice-capture-bar"]')).toBeNull()
    expect(dialog.querySelectorAll('button')).toHaveLength(4)
  })

  it('keeps an existing dictation active when opening and closing the viewer', async () => {
    const controls = {
      workspaceId: 'workspace-a',
      state: 'recording' as const,
      levels: [],
      stop: vi.fn(),
      send: vi.fn(),
      cancel: vi.fn(),
    }
    voiceCaptureControls.value = controls
    const dialog = await mountLightbox()
    expect(dialog.querySelector('[data-testid="voice-capture-bar"]')).not.toBeNull()
    button('Close image preview').click()
    wrapper.unmount()
    expect(voiceCaptureControls.value).toBe(controls)
    expect(controls.stop).not.toHaveBeenCalled()
    expect(controls.cancel).not.toHaveBeenCalled()
  })

  it('retains the missing image-reference notice during an active pointing dictation', async () => {
    voiceCaptureControls.value = {
      workspaceId: 'workspace-a',
      state: 'recording',
      levels: [],
      stop: vi.fn(),
      send: vi.fn(),
      cancel: vi.fn(),
    }
    activePointingWorkspace.value = 'workspace-a'
    const dialog = await mountLightbox()
    expect(dialog.textContent).toContain(
      'This image has no workspace path. Pointing cannot be linked to it.',
    )
    await wrapper.setProps({ path: '/workspace/design.png', workspaceId: 'workspace-a' })
    expect(dialog.textContent).not.toContain('This image has no workspace path.')
  })

  it('fits large images and never stretches small images on opening', async () => {
    const dialog = await mountLightbox(false)
    expect(button('Zoom in').disabled).toBe(true)
    await loadImage(dialog, 400, 200)
    expect(percent(dialog)).toBe('100%')
    expect(view(dialog)).toEqual({ x: 0, y: 0, scale: 1 })
    expect(dialog.querySelector('img')!.style.width).toBe('400px')
    expect(button('Zoom out').disabled).toBe(true)
  })

  it('fits using layout dimensions even during the scaled dialog opening animation', async () => {
    stageTransformScale = 0.95
    const dialog = await mountLightbox()
    expect(view(dialog)).toEqual({ x: 0, y: 0, scale: 0.5 })
  })

  it('zooms with controls and keyboard, respects limits, and fits with 0', async () => {
    const dialog = await mountLightbox()
    button('Zoom in').click()
    await nextTick()
    expect(percent(dialog)).toBe('67%')
    await key(dialog, '+')
    expect(percent(dialog)).toBe('75%')
    await key(dialog, '-')
    expect(percent(dialog)).toBe('67%')
    for (let i = 0; i < 20; i++) await key(dialog, '+')
    expect(percent(dialog)).toBe('800%')
    expect(button('Zoom in').disabled).toBe(true)
    await key(dialog, '0')
    expect(view(dialog)).toEqual({ x: 0, y: 0, scale: 0.5 })
    await key(dialog, '-')
    expect(percent(dialog)).toBe('50%')
    await key(dialog, '+', { ctrlKey: true })
    expect(percent(dialog)).toBe('50%')
  })

  it('uses small proportional wheel changes and keeps the pixel under the cursor fixed', async () => {
    const dialog = await mountLightbox()
    await wheel(dialog, -1)
    expect(view(dialog).scale).toBeCloseTo(0.5 * Math.exp(0.001))
    expect(view(dialog).scale).toBeLessThan(0.501)
    await wheel(dialog, -99)
    const zoomed = view(dialog)
    expect(zoomed.scale).toBeCloseTo(0.5 * Math.exp(0.1))
    expect((750 - 500 - zoomed.x) / zoomed.scale).toBeCloseTo((750 - 500) / 0.5)
    expect((525 - 350 - zoomed.y) / zoomed.scale).toBeCloseTo((525 - 350) / 0.5)
    await wheel(dialog, 100)
    expect(view(dialog).scale).toBeCloseTo(0.5)
    expect(view(dialog).x).toBeCloseTo(0)
    expect(view(dialog).y).toBeCloseTo(0)
    expect(dialog.querySelector('img')!.style.transition).toBe('')
  })

  it.each([
    { deltaY: -1, deltaMode: 1, normalized: 16 },
    { deltaY: -1, deltaMode: 2, normalized: 100 },
    { deltaY: -10000, deltaMode: 0, normalized: 100 },
  ])(
    'normalizes and caps wheel distances: $deltaMode / $deltaY',
    async ({ deltaY, deltaMode, normalized }) => {
      const dialog = await mountLightbox()
      await wheel(dialog, deltaY, { deltaMode })
      expect(view(dialog).scale).toBeCloseTo(0.5 * Math.exp(normalized * 0.001))
    },
  )

  it('handles trackpad pinch wheel events within the image instead of zooming the page', async () => {
    const dialog = await mountLightbox()
    const event = new WheelEvent('wheel', {
      deltaY: -2,
      ctrlKey: true,
      clientX: 520,
      clientY: 430,
      bubbles: true,
      cancelable: true,
    })
    viewport(dialog).dispatchEvent(event)
    await nextTick()
    expect(event.defaultPrevented).toBe(true)
    expect(view(dialog).scale).toBeCloseTo(0.5 * Math.exp(0.01))
  })

  it('keeps the currently viewed center when zooming with buttons', async () => {
    const dialog = await mountLightbox()
    await wheel(dialog, -100)
    const before = view(dialog)
    button('Zoom in').click()
    await nextTick()
    const after = view(dialog)
    expect(after.x / after.scale).toBeCloseTo(before.x / before.scale)
    expect(after.y / after.scale).toBeCloseTo(before.y / before.scale)
  })

  it('toggles a useful detail zoom and fit with double-click without closing', async () => {
    const dialog = await mountLightbox()
    await doubleClick(dialog, 750, 525)
    expect(view(dialog)).toEqual({ x: -250, y: -175, scale: 1 })
    await doubleClick(dialog, 750, 525)
    expect(view(dialog)).toEqual({ x: 0, y: 0, scale: 0.5 })
    expect(wrapper.emitted('close')).toBeUndefined()
  })

  it('pans directly, keeps all edges reachable, and continues dragging outside the viewport', async () => {
    const dialog = await mountLightbox()
    const stage = viewport(dialog)
    await doubleClick(dialog)
    await pointer(dialog, 'pointerdown', 1, 500, 350)
    expect(stage.setPointerCapture).toHaveBeenCalledWith(1)
    await pointer(dialog, 'pointermove', 1, 680, 440)
    expect(view(dialog)).toEqual({ x: 180, y: 90, scale: 1 })
    await pointer(dialog, 'pointerleave', 1, 1001, 701)
    await pointer(dialog, 'pointermove', 1, 5000, 5000)
    expect(view(dialog)).toEqual({ x: 500, y: 350, scale: 1 })
    // Reversing at an edge must respond immediately, without a drag dead zone.
    await pointer(dialog, 'pointermove', 1, 4990, 4990)
    expect(view(dialog)).toEqual({ x: 490, y: 340, scale: 1 })
    await pointer(dialog, 'pointermove', 1, -5000, -5000)
    expect(view(dialog)).toEqual({ x: -500, y: -350, scale: 1 })
    await pointer(dialog, 'pointerup', 1, -5000, -5000)
    expect(stage.releasePointerCapture).toHaveBeenCalledWith(1)
    await pointer(dialog, 'pointermove', 1, 500, 350)
    expect(view(dialog)).toEqual({ x: -500, y: -350, scale: 1 })
    stage.click()
    expect(wrapper.emitted('close')).toBeUndefined()
  })

  it('supports a moving two-finger pinch and continues with one-finger pan without jumping', async () => {
    const dialog = await mountLightbox()
    await pointer(dialog, 'pointerdown', 1, 400, 350)
    await pointer(dialog, 'pointerdown', 2, 600, 350)
    await pointer(dialog, 'pointermove', 2, 800, 350)
    expect(view(dialog)).toEqual({ x: 100, y: 0, scale: 1 })
    await pointer(dialog, 'pointerup', 2, 800, 350)
    await pointer(dialog, 'pointermove', 1, 430, 370)
    expect(view(dialog)).toEqual({ x: 130, y: 20, scale: 1 })
    await pointer(dialog, 'pointercancel', 1, 430, 370)
    await pointer(dialog, 'pointermove', 1, 500, 500)
    expect(view(dialog)).toEqual({ x: 130, y: 20, scale: 1 })
    expect(viewport(dialog).classList.contains('cursor-grabbing')).toBe(false)
  })

  it('allows keyboard pan and clamps it to the image edges', async () => {
    const dialog = await mountLightbox()
    await doubleClick(dialog)
    await key(dialog, 'ArrowLeft')
    await key(dialog, 'ArrowUp', { shiftKey: true })
    expect(view(dialog)).toEqual({ x: 50, y: 150, scale: 1 })
    for (let i = 0; i < 10; i++) await key(dialog, 'ArrowRight', { shiftKey: true })
    expect(view(dialog).x).toBe(-500)
  })

  it('refits on resize while fitted and preserves detail zoom while inspecting', async () => {
    const dialog = await mountLightbox()
    stageRect = new DOMRect(20, 80, 500, 350)
    resizeStage()
    await nextTick()
    expect(view(dialog)).toEqual({ x: 0, y: 0, scale: 0.25 })
    await doubleClick(dialog, 250, 175)
    expect(view(dialog).scale).toBe(1)
    stageRect = new DOMRect(20, 80, 800, 560)
    resizeStage()
    await nextTick()
    expect(view(dialog)).toEqual({ x: 0, y: 0, scale: 1 })
  })

  it('emits close only from the close button or Escape', async () => {
    const dialog = await mountLightbox()
    viewport(dialog).click()
    expect(wrapper.emitted('close')).toBeUndefined()
    button('Close image preview').click()
    await nextTick()
    expect(wrapper.emitted('close')).toHaveLength(1)
    await key(dialog, 'Escape')
    expect(wrapper.emitted('close')).toHaveLength(2)
  })

  it('handles load failures and recovers when the image source changes', async () => {
    const dialog = await mountLightbox()
    dialog.querySelector('img')!.dispatchEvent(new Event('error'))
    await nextTick()
    expect(dialog.querySelector('[role="status"]')?.textContent).toContain(
      'Image could not be loaded.',
    )
    expect(button('Zoom in').disabled).toBe(true)
    expect(button('Zoom out').disabled).toBe(true)
    expect(button('Download image').disabled).toBe(true)
    expect(button('Close image preview').disabled).toBe(false)
    await wrapper.setProps({ src: 'blob:replacement' })
    await loadImage(dialog, 400, 200)
    expect(percent(dialog)).toBe('100%')
    expect(button('Download image').disabled).toBe(false)
    expect(dialog.querySelector('[role="status"]')).toBeNull()
  })

  it.each([
    {
      src: 'blob:workspace-image',
      path: '/workspace/design.png',
      mime: 'image/png',
      filename: 'design.png',
    },
    {
      src: 'data:image/jpeg;base64,abc',
      path: undefined,
      mime: 'image/jpeg',
      filename: 'image.jpg',
    },
    {
      src: 'https://example.com/chat%20photo.webp?signature=abc',
      path: undefined,
      mime: 'image/webp',
      filename: 'chat photo.webp',
    },
  ])(
    'downloads $src with the correct name and releases the browser URL',
    async ({ src, path, mime, filename }) => {
      const dialog = await mountLightbox()
      await wrapper.setProps({ src, path })
      await loadImage(dialog)
      const blob = new Blob(['image bytes'], { type: mime })
      vi.mocked(fetch).mockResolvedValue({ ok: true, blob: async () => blob } as Response)
      const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
        this: HTMLAnchorElement,
      ) {
        expect(this.download).toBe(filename)
        expect(this.href).toBe('blob:download')
        expect(this.isConnected).toBe(true)
      })
      vi.useFakeTimers()
      button('Download image').click()
      await flushPromises()
      expect(fetch).toHaveBeenCalledWith(src, { signal: expect.any(AbortSignal) })
      expect(createObjectURL).toHaveBeenCalledWith(blob)
      expect(click).toHaveBeenCalledOnce()
      expect(button('Download image').disabled).toBe(false)
      expect(document.querySelector('a[download]')).toBeNull()
      expect(revokeObjectURL).not.toHaveBeenCalled()
      vi.advanceTimersByTime(30_000)
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:download')
    },
  )

  it('reports failed downloads and allows retrying', async () => {
    await mountLightbox()
    vi.mocked(fetch).mockResolvedValueOnce({ ok: false } as Response)
    button('Download image').click()
    await flushPromises()
    expect(notifyError).toHaveBeenCalledWith('Download failed', expect.any(String))
    expect(createObjectURL).not.toHaveBeenCalled()
    expect(button('Download image').disabled).toBe(false)
  })

  it('aborts a pending download on unmount without starting a stale download', async () => {
    await mountLightbox()
    let resolveDownload!: (response: Response) => void
    vi.mocked(fetch).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveDownload = resolve
      }),
    )
    button('Download image').click()
    await nextTick()
    expect(button('Download image').disabled).toBe(true)
    const signal = vi.mocked(fetch).mock.calls[0]![1]!.signal!
    wrapper.unmount()
    expect(signal.aborted).toBe(true)
    resolveDownload({ ok: true, blob: async () => new Blob(['image']) } as Response)
    await flushPromises()
    expect(createObjectURL).not.toHaveBeenCalled()
    expect(notifyError).not.toHaveBeenCalled()
  })

  it('disconnects observation on unmount and ignores keys outside the modal', async () => {
    const dialog = await mountLightbox()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: '+', bubbles: true }))
    await nextTick()
    expect(percent(dialog)).toBe('50%')
    wrapper.unmount()
    expect(disconnect).toHaveBeenCalled()
  })
})
