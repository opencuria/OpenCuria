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
  iframe.dataset.voiceDesktop = ''
  iframe.dataset.voiceWorkspace = 'workspace-a'
  iframe.getBoundingClientRect = () => ({ width: 960, height: 540 }) as DOMRect
  document.body.append(iframe)
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
        width: 1920,
        height: 1080,
        captured_at: performance.timeOrigin + performance.now(),
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
    vi.unstubAllGlobals()
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
    expect((await promise).blob.type).toBe('image/jpeg')
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

  it('captures on HTTP browsers that expose getRandomValues without randomUUID', async () => {
    vi.stubGlobal('crypto', { getRandomValues: (bytes: Uint8Array) => bytes.fill(255) })
    const { iframe, send } = viewer()
    const promise = captureDesktopFrame(iframe, new AbortController().signal)
    const requestId = send.mock.calls[0]![0].requestId as string
    expect(requestId).toBe('ffffffff-ffff-4fff-bfff-ffffffffffff')
    frameReply(iframe, requestId)
    expect((await promise).blob.type).toBe('image/jpeg')
    expect(vi.getTimerCount()).toBe(0)
  })

  it('fails capture without sending a request when secure randomness is unavailable', async () => {
    vi.stubGlobal('crypto', undefined)
    const { iframe, send } = viewer()
    await expect(captureDesktopFrame(iframe, new AbortController().signal)).rejects.toThrow(
      'Secure desktop capture IDs are unavailable',
    )
    expect(send).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('ignores a frame observed after stop and never uploads it', async () => {
    const { iframe, send } = viewer()
    unregister = registerPointingDesktop('workspace-a', () => iframe)
    recording = startPointing('workspace-a', recordingId, performance.now())
    await vi.advanceTimersByTimeAsync(0)
    const result = recording.stop()
    frameReply(iframe, frameRequests(send)[0]!.requestId)
    expect((await result).frames).toEqual([])
    expect(transport.send).not.toHaveBeenCalled()
  })

  it('bounds click floods and reports an explicit failure instead of growing indefinitely', async () => {
    recording = startPointing('workspace-a', recordingId, performance.now())
    for (let index = 0; index < 6100; index++) move('pointerdown')
    await expect(recording.stop()).rejects.toThrow('event limit')
    expect(activePointingWorkspace.value).toBeNull()
  })

  it('captures fresh frames while uploads drain and observes real iframe clicks without intercepting input', async () => {
    const { iframe, send } = viewer()
    unregister = registerPointingDesktop('workspace-a', () => iframe)
    let completeUpload!: () => void
    transport.track.mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          completeUpload = resolve
        }),
    )
    recording = startPointing('workspace-a', recordingId, performance.now())
    await vi.advanceTimersByTimeAsync(0)
    expect(frameRequests(send)).toHaveLength(1)
    frameReply(iframe, frameRequests(send)[0]!.requestId)
    await vi.advanceTimersByTimeAsync(100)
    expect(transport.track).toHaveBeenCalledOnce()
    const point = {
      type: 'opencuria:voice-pointer',
      sessionId: recordingId,
      event: 'down',
      observed_at: performance.timeOrigin + performance.now(),
      width: 1920,
      height: 1080,
      x: 1500,
      y: 700,
    }
    const dispatch = (
      data = point,
      origin = 'https://desktop.test',
      source = iframe.contentWindow,
    ) => window.dispatchEvent(new MessageEvent('message', { source, origin, data }))
    dispatch({ ...point, sessionId: 'other-recording' })
    dispatch(point, 'https://evil.test')
    dispatch(point, 'https://desktop.test', window)
    dispatch()
    await vi.advanceTimersByTimeAsync(400)
    expect(frameRequests(send)).toHaveLength(2) // first upload is still pending
    frameReply(iframe, frameRequests(send)[1]!.requestId)
    await vi.advanceTimersByTimeAsync(0)
    const resultPromise = recording.stop()
    completeUpload()
    const result = await resultPromise
    expect(result.frames).toEqual([
      { t_ms: 0, path: `/workspace/.opencuria/recordings/${recordingId}/frames/000000.jpg` },
      { t_ms: 500, path: `/workspace/.opencuria/recordings/${recordingId}/frames/000001.jpg` },
    ])
    expect(result.pointers.filter((item) => item.event === 'down')).toEqual([
      {
        t_ms: 100,
        event: 'down',
        x: 1500,
        y: 700,
        target: { kind: 'desktop', path: result.frames[0]!.path, width: 1920, height: 1080 },
      },
    ])
    expect(send).toHaveBeenLastCalledWith(
      expect.objectContaining({ type: 'opencuria:voice-observe', enabled: false }),
      'https://desktop.test',
    )
    expect(activePointingWorkspace.value).toBeNull()
  })

  it('never attaches pointer coordinates to a frame with different natural dimensions', async () => {
    const { iframe, send } = viewer()
    unregister = registerPointingDesktop('workspace-a', () => iframe)
    recording = startPointing('workspace-a', recordingId, performance.now())
    await vi.advanceTimersByTimeAsync(0)
    frameReply(iframe, frameRequests(send)[0]!.requestId)
    await vi.advanceTimersByTimeAsync(100)
    window.dispatchEvent(
      new MessageEvent('message', {
        source: iframe.contentWindow,
        origin: 'https://desktop.test',
        data: {
          type: 'opencuria:voice-pointer',
          sessionId: recordingId,
          event: 'down',
          observed_at: performance.timeOrigin + performance.now(),
          width: 700,
          height: 230,
          x: 350,
          y: 115,
        },
      }),
    )
    const result = await recording.stop()
    expect(result.frames).toHaveLength(1)
    expect(result.pointers.find((point) => point.event === 'down')?.target).toBeNull()
  })

  it('does not capture a hidden or disconnected viewer', async () => {
    const { iframe, send } = viewer()
    iframe.removeAttribute('data-voice-desktop')
    unregister = registerPointingDesktop('workspace-a', () => iframe)
    recording = startPointing('workspace-a', recordingId, performance.now())
    await vi.advanceTimersByTimeAsync(500)
    expect(frameRequests(send)).toHaveLength(0)
    iframe.dataset.voiceDesktop = ''
    iframe.style.visibility = 'hidden'
    await vi.advanceTimersByTimeAsync(500)
    expect(frameRequests(send)).toHaveLength(0)
    await recording.stop()
  })

  it('rejects malformed framebuffer dimensions instead of assigning workspace display dimensions', async () => {
    const { iframe, send } = viewer()
    const promise = captureDesktopFrame(iframe, new AbortController().signal)
    const rejected = expect(promise).rejects.toThrow('could not be captured')
    frameReply(iframe, frameRequests(send)[0]!.requestId, {
      data: {
        type: 'opencuria:voice-frame',
        requestId: frameRequests(send)[0]!.requestId,
        blob: new Blob(['jpeg'], { type: 'image/jpeg' }),
        width: 0,
        height: 1080,
        captured_at: performance.timeOrigin,
      },
    })
    await rejected
  })
})

function frameRequests(send: ReturnType<typeof vi.fn>): { requestId: string }[] {
  return send.mock.calls
    .map((call) => call[0])
    .filter((data) => data.type === 'opencuria:voice-frame')
}
