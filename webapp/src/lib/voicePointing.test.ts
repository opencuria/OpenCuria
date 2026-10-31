import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  activePointingWorkspace,
  captureDesktopFrame,
  imagePoint,
  registerPointingDesktop,
  startPointing,
} from './voicePointing'

const transport = vi.hoisted(() => ({
  send: vi.fn(),
  track: vi.fn(async () => {}),
  fail: vi.fn(),
  cancel: vi.fn(),
}))
vi.mock('@/stores/fileExplorer', () => ({
  useFileExplorerStore: () => ({
    trackAndUpload: transport.track,
    failUpload: transport.fail,
    cancelUpload: transport.cancel,
  }),
}))
vi.mock('@/services/socket', () => ({ sendFilesUpload: transport.send }))
vi.mock('@/lib/chatUpload', () => ({ fileToBase64: vi.fn(async () => 'anBlZw==') }))

const recordingId = '11111111-1111-4111-8111-111111111111'
let recording: ReturnType<typeof startPointing> | null = null
let unregister: (() => void) | null = null
let hit: Element | null = null

function move(type: string, x = 200, y = 100): void {
  document.dispatchEvent(new MouseEvent(type, { clientX: x, clientY: y, bubbles: true }))
}

function viewer(): { iframe: HTMLIFrameElement; send: ReturnType<typeof vi.fn> } {
  const iframe = document.createElement('iframe')
  iframe.src = 'https://desktop.test/viewer'
  const send = vi.fn()
  Object.defineProperty(iframe, 'contentWindow', { value: { postMessage: send } })
  return { iframe, send }
}

function frameReply(iframe: HTMLIFrameElement, requestId: string, overrides = {}): void {
  window.dispatchEvent(
    new MessageEvent('message', {
      origin: 'https://desktop.test',
      source: iframe.contentWindow,
      data: {
        type: 'opencuria:voice-frame',
        requestId,
        blob: new Blob(['jpeg'], { type: 'image/jpeg' }),
      },
      ...overrides,
    }),
  )
}

describe('voice pointing', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
    Object.defineProperty(document, 'elementFromPoint', { configurable: true, value: () => hit })
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
    hit = null
  })

  afterEach(() => {
    recording?.cancel()
    recording = null
    unregister?.()
    unregister = null
    document.body.innerHTML = ''
    activePointingWorkspace.value = null
    vi.useRealTimers()
  })

  it('maps zoomed/scaled rectangles to natural pixels and rejects letterbox/outside points', () => {
    const rect = { left: -100, top: 10, width: 800, height: 400 }
    expect(imagePoint(rect, 1600, 800, 200, 110)).toEqual({ x: 600, y: 200 })
    expect(imagePoint(rect, 1600, 800, 700, 110)).toBeNull()
    expect(imagePoint({ ...rect, width: 0 }, 1600, 800, 200, 110)).toBeNull()
    expect(imagePoint(rect, 0, 0, 200, 110)).toBeNull()
  })

  it('samples a marked workspace image, clicks and explicit leaving on the shared clock', async () => {
    const image = document.createElement('img')
    image.dataset.voiceImagePath = '/workspace/design.png'
    image.dataset.voiceWorkspace = 'workspace-a'
    Object.defineProperties(image, { naturalWidth: { value: 1000 }, naturalHeight: { value: 500 } })
    image.getBoundingClientRect = () => ({ left: 100, top: 50, width: 400, height: 200 }) as DOMRect
    hit = image
    recording = startPointing('workspace-a', recordingId, performance.now())
    move('pointermove')
    await vi.advanceTimersByTimeAsync(100)
    move('pointerdown')
    move('pointerup')
    window.dispatchEvent(new Event('blur'))
    const result = await recording.stop()
    expect(result.pointers[0]).toEqual({
      t_ms: 100,
      event: 'move',
      x: 250,
      y: 125,
      target: { kind: 'image', path: '/workspace/design.png', width: 1000, height: 500 },
    })
    expect(result.pointers.map((point) => point.event)).toEqual([
      'move',
      'down',
      'up',
      'leave',
      'leave',
    ])
    expect(result.pointers[result.pointers.length - 1]?.target).toBeNull()
    expect(activePointingWorkspace.value).toBeNull()
    expect(transport.send).not.toHaveBeenCalled()
  })

  it('never logs browser URLs, escaped paths or images from another workspace', async () => {
    const image = document.createElement('img')
    image.dataset.voiceImagePath = '/workspace/../secret.png'
    image.dataset.voiceWorkspace = 'workspace-a'
    hit = image
    recording = startPointing('workspace-a', recordingId, performance.now())
    move('pointermove')
    await vi.advanceTimersByTimeAsync(100)
    image.dataset.voiceImagePath = 'blob:https://example.test/secret'
    await vi.advanceTimersByTimeAsync(100)
    image.dataset.voiceImagePath = '/workspace/design.png'
    image.dataset.voiceWorkspace = 'workspace-b'
    await vi.advanceTimersByTimeAsync(100)
    const result = await recording.stop()
    expect(result.pointers.every((point) => point.target === null)).toBe(true)
  })

  it('rejects concurrent recordings and cancels all listeners and timers', async () => {
    recording = startPointing('workspace-a', recordingId, performance.now())
    expect(() => startPointing('workspace-b', recordingId, performance.now())).toThrow(
      'already active',
    )
    recording.cancel()
    move('pointerdown')
    await vi.advanceTimersByTimeAsync(1000)
    await expect(recording.stop()).rejects.toThrow('cancelled')
    expect(transport.send).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('accepts capture replies only from the exact viewer, origin and request', async () => {
    const { iframe, send } = viewer()
    const abort = new AbortController()
    const promise = captureDesktopFrame(iframe, abort.signal)
    const requestId = send.mock.calls[0]![0].requestId as string
    let settled = false
    void promise.then(() => {
      settled = true
    })
    frameReply(iframe, requestId, { origin: 'https://evil.test' })
    frameReply(iframe, 'wrong-request')
    frameReply(iframe, requestId, { source: window })
    await Promise.resolve()
    expect(settled).toBe(false)
    frameReply(iframe, requestId)
    expect((await promise).type).toBe('image/jpeg')
    expect(vi.getTimerCount()).toBe(0)
  })

  it('times out frame capture and clears its listener on abort', async () => {
    const { iframe } = viewer()
    const abort = new AbortController()
    const timedOut = expect(captureDesktopFrame(iframe, abort.signal)).rejects.toThrow('timed out')
    await vi.advanceTimersByTimeAsync(5000)
    await timedOut
    const cancelled = expect(captureDesktopFrame(iframe, abort.signal)).rejects.toThrow('cancelled')
    abort.abort()
    await cancelled
    expect(vi.getTimerCount()).toBe(0)
  })

  it('ignores a frame observed after stop and never uploads it', async () => {
    const { iframe, send } = viewer()
    unregister = registerPointingDesktop('workspace-a', () => iframe)
    const overlay = document.createElement('div')
    overlay.dataset.voiceDesktop = ''
    overlay.dataset.voiceWorkspace = 'workspace-a'
    overlay.getBoundingClientRect = () => ({ width: 960, height: 540 }) as DOMRect
    document.body.append(overlay)
    recording = startPointing('workspace-a', recordingId, performance.now())
    await vi.advanceTimersByTimeAsync(0)
    const result = recording.stop()
    frameReply(iframe, send.mock.calls[0]![0].requestId)
    expect((await result).frames).toEqual([])
    expect(transport.send).not.toHaveBeenCalled()
  })

  it('bounds click floods and reports an explicit failure instead of growing indefinitely', async () => {
    recording = startPointing('workspace-a', recordingId, performance.now())
    for (let index = 0; index < 6100; index++) move('pointerdown')
    await expect(recording.stop()).rejects.toThrow('event limit')
    expect(activePointingWorkspace.value).toBeNull()
  })

  it('serializes desktop captures, saves frames and drains the upload on stop', async () => {
    const { iframe, send } = viewer()
    unregister = registerPointingDesktop('workspace-a', () => iframe)
    const overlay = document.createElement('div')
    overlay.dataset.voiceDesktop = ''
    overlay.dataset.voiceWorkspace = 'workspace-a'
    overlay.dataset.voiceWidth = '1920'
    overlay.dataset.voiceHeight = '1080'
    overlay.getBoundingClientRect = () => ({ left: 0, top: 0, width: 960, height: 540 }) as DOMRect
    document.body.append(overlay)
    hit = overlay
    let completeUpload!: () => void
    transport.track.mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          completeUpload = resolve
        }),
    )
    recording = startPointing('workspace-a', recordingId, performance.now())
    await vi.advanceTimersByTimeAsync(0)
    expect(send).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(500)
    expect(send).toHaveBeenCalledOnce()
    frameReply(iframe, send.mock.calls[0]![0].requestId)
    await Promise.resolve()
    await Promise.resolve()
    const resultPromise = recording.stop()
    expect(transport.track).toHaveBeenCalledOnce()
    completeUpload()
    const result = await resultPromise
    expect(result.frames).toEqual([
      { t_ms: 500, path: `/workspace/.opencuria/recordings/${recordingId}/frames/000000.jpg` },
    ])
    expect(transport.send).toHaveBeenCalledOnce()
    expect(activePointingWorkspace.value).toBeNull()
  })
})
