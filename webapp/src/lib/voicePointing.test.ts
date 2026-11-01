import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { VOICE_MAX_DURATION_MS, VOICE_MAX_POINTERS } from './voiceLimits'
import {
  activePointingWorkspace,
  captureDesktopFrame,
  imagePoint,
  pointingEngaged,
  pointingError,
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

function pointAt(iframe: HTMLIFrameElement, overrides = {}): void {
  window.dispatchEvent(
    new MessageEvent('message', {
      origin: 'https://desktop.test',
      source: iframe.contentWindow,
      data: {
        type: 'opencuria:voice-pointer',
        sessionId: recordingId,
        event: 'move',
        observed_at: performance.timeOrigin + performance.now(),
        width: 1920,
        height: 1080,
        x: 960,
        y: 540,
        ...overrides,
      },
    }),
  )
}

function frameReply(iframe: HTMLIFrameElement, requestId: string, overrides = {}): void {
  window.dispatchEvent(
    new MessageEvent('message', {
      origin: 'https://desktop.test',
      source: iframe.contentWindow,
      data: {
        type: 'opencuria:voice-frame',
        requestId,
        frameId: requestId,
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
    pointingEngaged.value = false
    pointingError.value = null
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
    expect(pointingEngaged.value).toBe(true)
    const result = (await recording.stop())!
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
    expect(pointingEngaged.value).toBe(false)
    // Nothing recordable was pointed at, so the dictation stays plain.
    expect(await recording.stop()).toBeNull()
  })

  it('stays passive for plain dictation: no frames, no uploads and no result', async () => {
    const { iframe, send } = viewer()
    unregister = registerPointingDesktop('workspace-a', () => iframe)
    recording = startPointing('workspace-a', recordingId, performance.now())
    move('pointermove')
    await vi.advanceTimersByTimeAsync(2000)
    expect(frameRequests(send)).toHaveLength(0)
    // The viewer is still observed so pointing can start at any moment.
    expect(send).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'opencuria:voice-observe', enabled: true, frameWidth: 0 }),
      'https://desktop.test',
    )
    expect(await recording.stop()).toBeNull()
    expect(transport.track).not.toHaveBeenCalled()
    expect(pointingEngaged.value).toBe(false)
  })

  it('shows the laser only on a recordable image and restores the cursor on leave and stop', async () => {
    const image = document.createElement('img')
    image.dataset.voiceImagePath = '/workspace/design.png'
    image.dataset.voiceWorkspace = 'workspace-a'
    Object.defineProperties(image, { naturalWidth: { value: 1000 }, naturalHeight: { value: 500 } })
    image.getBoundingClientRect = () => ({ left: 100, top: 50, width: 400, height: 200 }) as DOMRect
    document.body.append(image)
    hit = image
    move('pointermove')
    expect(image.hasAttribute('data-voice-pointing-cursor')).toBe(false)

    recording = startPointing('workspace-a', recordingId, performance.now())
    move('pointermove')
    expect(image.hasAttribute('data-voice-pointing-cursor')).toBe(true)
    const cursor = getComputedStyle(image).cursor
    expect(cursor).toContain('data:image/svg+xml')
    expect(cursor).toContain('12 12')

    for (const path of ['blob:https://example.test/image', '/workspace/../secret.png']) {
      image.dataset.voiceImagePath = path
      move('pointermove')
      expect(image.hasAttribute('data-voice-pointing-cursor')).toBe(false)
    }
    image.dataset.voiceImagePath = '/workspace/design.png'
    image.dataset.voiceWorkspace = 'workspace-b'
    move('pointermove')
    expect(image.hasAttribute('data-voice-pointing-cursor')).toBe(false)
    image.dataset.voiceWorkspace = 'workspace-a'
    move('pointermove', 0, 0) // Outside the actual image pixels.
    expect(image.hasAttribute('data-voice-pointing-cursor')).toBe(false)
    move('pointermove')
    expect(image.hasAttribute('data-voice-pointing-cursor')).toBe(true)
    hit = document.createElement('button')
    move('pointermove')
    expect(image.hasAttribute('data-voice-pointing-cursor')).toBe(false)
    hit = image
    move('pointermove')
    window.dispatchEvent(new Event('blur'))
    expect(image.hasAttribute('data-voice-pointing-cursor')).toBe(false)
    move('pointermove')
    await recording.stop()
    expect(image.hasAttribute('data-voice-pointing-cursor')).toBe(false)
    expect(getComputedStyle(image).cursor).not.toContain('data:image/svg+xml')
  })

  it('removes the laser immediately when capture fails or is cancelled', async () => {
    const image = document.createElement('img')
    image.dataset.voiceImagePath = '/workspace/design.png'
    image.dataset.voiceWorkspace = 'workspace-a'
    Object.defineProperties(image, { naturalWidth: { value: 1000 }, naturalHeight: { value: 500 } })
    image.getBoundingClientRect = () => ({ left: 100, top: 50, width: 400, height: 200 }) as DOMRect
    hit = image
    recording = startPointing('workspace-a', recordingId, performance.now())
    move('pointermove')
    expect(image.hasAttribute('data-voice-pointing-cursor')).toBe(true)
    for (let index = 0; index <= VOICE_MAX_POINTERS; index++) move('pointerdown')
    expect(image.hasAttribute('data-voice-pointing-cursor')).toBe(false)
    recording.cancel()
    recording = startPointing('workspace-a', recordingId, performance.now())
    move('pointermove')
    expect(image.hasAttribute('data-voice-pointing-cursor')).toBe(true)
    recording.cancel()
    expect(image.hasAttribute('data-voice-pointing-cursor')).toBe(false)
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
    expect(await promise).toMatchObject({ blob: expect.any(Blob), frameId: requestId })
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
    expect(await promise).toMatchObject({ blob: expect.any(Blob), frameId: requestId })
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
    pointAt(iframe)
    const result = recording.stop()
    frameReply(iframe, frameRequests(send)[0]!.requestId)
    expect(await result).toBeNull()
    expect(transport.send).not.toHaveBeenCalled()
  })

  it('bounds click floods and reports an explicit failure instead of growing indefinitely', async () => {
    const image = document.createElement('img')
    image.dataset.voiceImagePath = '/workspace/design.png'
    image.dataset.voiceWorkspace = 'workspace-a'
    Object.defineProperties(image, { naturalWidth: { value: 1000 }, naturalHeight: { value: 500 } })
    image.getBoundingClientRect = () => ({ left: 100, top: 50, width: 400, height: 200 }) as DOMRect
    hit = image
    recording = startPointing('workspace-a', recordingId, performance.now())
    for (let index = 0; index <= VOICE_MAX_POINTERS; index++) move('pointerdown')
    expect(pointingError.value).toContain('event limit')
    await expect(recording.stop()).rejects.toThrow('event limit')
    expect(activePointingWorkspace.value).toBeNull()
  })

  it('never fails plain dictation because of an unused pointing observer', async () => {
    recording = startPointing('workspace-a', recordingId, performance.now())
    for (let index = 0; index < 6100; index++) move('pointerdown')
    expect(pointingError.value).toBeNull()
    expect(await recording.stop()).toBeNull()
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
    expect(frameRequests(send)).toHaveLength(0)
    pointAt(iframe) // The first pointer on the desktop starts capture immediately.
    expect(frameRequests(send)).toHaveLength(1)
    frameReply(iframe, frameRequests(send)[0]!.requestId)
    await vi.advanceTimersByTimeAsync(100)
    expect(transport.track).toHaveBeenCalledOnce()
    expect(pointingEngaged.value).toBe(true)
    expect(send).toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'opencuria:voice-observe',
        enabled: true,
        cursor: expect.stringContaining('data:image/svg+xml'),
        frameWidth: 1920,
        frameHeight: 1080,
      }),
      'https://desktop.test',
    )
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
    const result = (await resultPromise)!
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
    pointAt(iframe)
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
    const result = (await recording.stop())!
    expect(result.frames).toHaveLength(1)
    expect(result.pointers.find((point) => point.event === 'down')?.target).toBeNull()
  })

  it('does not capture a hidden or disconnected viewer', async () => {
    const { iframe, send } = viewer()
    unregister = registerPointingDesktop('workspace-a', () => iframe)
    recording = startPointing('workspace-a', recordingId, performance.now())
    await vi.advanceTimersByTimeAsync(0)
    pointAt(iframe)
    frameReply(iframe, frameRequests(send)[0]!.requestId)
    await vi.advanceTimersByTimeAsync(100)
    iframe.removeAttribute('data-voice-desktop')
    await vi.advanceTimersByTimeAsync(500)
    expect(frameRequests(send)).toHaveLength(1)
    iframe.dataset.voiceDesktop = ''
    iframe.style.visibility = 'hidden'
    await vi.advanceTimersByTimeAsync(500)
    expect(frameRequests(send)).toHaveLength(1)
    expect((await recording.stop())!.frames).toHaveLength(1)
  })

  it('retains the previous frame and pointer target when the desktop is unchanged', async () => {
    const { iframe, send } = viewer()
    unregister = registerPointingDesktop('workspace-a', () => iframe)
    recording = startPointing('workspace-a', recordingId, performance.now())
    await vi.advanceTimersByTimeAsync(0)
    pointAt(iframe)
    const firstId = frameRequests(send)[0]!.requestId
    frameReply(iframe, firstId)
    await vi.advanceTimersByTimeAsync(500)
    const request = frameRequests(send)[1]!
    expect(request.previousFrameId).toBe(firstId)
    frameReply(iframe, request.requestId, {
      data: {
        type: 'opencuria:voice-frame',
        requestId: request.requestId,
        unchanged: true,
        captured_at: performance.timeOrigin + performance.now(),
      },
    })
    await vi.advanceTimersByTimeAsync(100)
    pointAt(iframe, { event: 'down' })
    const result = (await recording.stop())!
    expect(result.frames).toHaveLength(1)
    expect(transport.send).toHaveBeenCalledOnce()
    expect(result.pointers.find((point) => point.event === 'down')?.target?.path).toBe(
      result.frames[0]!.path,
    )
    expect(vi.getTimerCount()).toBe(0)
  })

  it('rejects an unchanged reply without an accepted baseline', async () => {
    const { iframe, send } = viewer()
    const promise = captureDesktopFrame(iframe, new AbortController().signal)
    const rejected = expect(promise).rejects.toThrow('could not be captured')
    frameReply(iframe, frameRequests(send)[0]!.requestId, {
      data: {
        type: 'opencuria:voice-frame',
        requestId: frameRequests(send)[0]!.requestId,
        unchanged: true,
        captured_at: performance.timeOrigin + performance.now(),
      },
    })
    await rejected
  })

  for (const resume of ['speech', 'desktop movement', 'parent movement', 'click'] as const) {
    it(`backs off changing frames to 30s despite stationary heartbeats and resumes on ${resume}`, async () => {
      const { iframe, send } = viewer()
      const times: number[] = []
      send.mockImplementation((data) => {
        if (data.type !== 'opencuria:voice-frame') return
        times.push(performance.now())
        // Asynchronous replies match the real postMessage boundary.
        queueMicrotask(() => frameReply(iframe, data.requestId))
      })
      unregister = registerPointingDesktop('workspace-a', () => iframe)
      recording = startPointing('workspace-a', recordingId, performance.now())
      await vi.advanceTimersByTimeAsync(0)
      pointAt(iframe)
      const heartbeat = setInterval(() => pointAt(iframe), 100)
      await vi.advanceTimersByTimeAsync(120_000)
      clearInterval(heartbeat)
      const gaps = times.slice(1).map((time, index) => time - times[index]!)
      expect(gaps.slice(0, 8)).toEqual(Array(8).fill(500))
      expect(gaps).toContain(1000)
      expect(gaps).toContain(2000)
      expect(gaps).toContain(4000)
      expect(gaps).toContain(8000)
      expect(gaps).toContain(16000)
      expect(gaps.slice(-2)).toEqual([30_000, 30_000])
      expect(transport.send).toHaveBeenCalledTimes(times.length)
      const beforeResume = times.length
      if (resume === 'speech') recording.audioActivity()
      else if (resume === 'desktop movement') pointAt(iframe, { x: 1000 })
      else if (resume === 'click') pointAt(iframe, { event: 'down' })
      else move('pointermove')
      await vi.advanceTimersByTimeAsync(500)
      expect(times).toHaveLength(beforeResume + 1)
      await vi.advanceTimersByTimeAsync(500)
      expect(times).toHaveLength(beforeResume + 2)
      expect(pointingError.value).toBeNull()
      const result = (await recording.stop())!
      expect(result.frames).toHaveLength(times.length)
      expect(vi.getTimerCount()).toBe(0)
    })
  }

  it('keeps the fast cadence during speech even with a stationary mouse', async () => {
    const { iframe, send } = viewer()
    send.mockImplementation((data) => {
      if (data.type === 'opencuria:voice-frame')
        queueMicrotask(() => frameReply(iframe, data.requestId))
    })
    unregister = registerPointingDesktop('workspace-a', () => iframe)
    recording = startPointing('workspace-a', recordingId, performance.now())
    await vi.advanceTimersByTimeAsync(0)
    pointAt(iframe)
    const speech = setInterval(() => recording!.audioActivity(), 100)
    await vi.advanceTimersByTimeAsync(40_000)
    clearInterval(speech)
    expect(frameRequests(send)).toHaveLength(81)
    expect((await recording.stop())!.frames).toHaveLength(81)
  })

  it('records ten minutes of changing desktop content, movement and clicks within the limits', async () => {
    const { iframe, send } = viewer()
    send.mockImplementation((data) => {
      if (data.type !== 'opencuria:voice-frame') return
      // Account for 200 KiB JPEGs without allocating 240 MiB in this unit test.
      const blob = new Blob(['jpeg'], { type: 'image/jpeg' })
      Object.defineProperty(blob, 'size', { value: 200 * 1024 })
      queueMicrotask(() => frameReply(iframe, data.requestId, {
        data: { type: data.type, requestId: data.requestId, frameId: data.requestId,
          blob, width: 1920, height: 1080,
          captured_at: performance.timeOrigin + performance.now() },
      }))
    })
    unregister = registerPointingDesktop('workspace-a', () => iframe)
    recording = startPointing('workspace-a', recordingId, performance.now())
    await vi.advanceTimersByTimeAsync(0)
    pointAt(iframe)
    const activity = setInterval(() => {
      const x = Math.floor(performance.now() / 100) % 1920
      pointAt(iframe, { x })
      if (performance.now() % 1000 === 0) {
        pointAt(iframe, { event: 'down', x })
        pointAt(iframe, { event: 'up', x })
      }
      recording!.audioActivity()
    }, 100)
    await vi.advanceTimersByTimeAsync(VOICE_MAX_DURATION_MS)
    clearInterval(activity)
    expect(pointingError.value).toBeNull()
    const result = (await recording.stop())!
    expect(result.frames).toHaveLength(1201)
    const lastFrame = result.frames[result.frames.length - 1]!
    expect(lastFrame.t_ms).toBe(VOICE_MAX_DURATION_MS)
    expect(result.pointers.length).toBeGreaterThan(6000)
    expect(result.pointers.length).toBeLessThan(VOICE_MAX_POINTERS)
    const clicks = result.pointers.filter((point) => point.event === 'down')
    const lastClick = clicks[clicks.length - 1]!
    expect(lastClick.t_ms).toBe(VOICE_MAX_DURATION_MS)
    expect(lastClick.target?.kind).toBe('desktop')
    expect(lastClick.target?.path).toBe(lastFrame.path)
    expect(transport.send).toHaveBeenCalledTimes(1201)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('holds an unchanged image position for ten minutes and preserves later clicks and transitions', async () => {
    const image = document.createElement('img')
    image.dataset.voiceImagePath = '/workspace/design.png'
    image.dataset.voiceWorkspace = 'workspace-a'
    Object.defineProperties(image, { naturalWidth: { value: 1000 }, naturalHeight: { value: 500 } })
    image.getBoundingClientRect = () => ({ left: 100, top: 50, width: 400, height: 200 }) as DOMRect
    hit = image
    recording = startPointing('workspace-a', recordingId, performance.now())
    move('pointermove')
    await vi.advanceTimersByTimeAsync(VOICE_MAX_DURATION_MS - 100)
    move('pointerdown')
    move('pointerup')
    image.dataset.voiceImagePath = '/workspace/other.png'
    await vi.advanceTimersByTimeAsync(100)
    const result = (await recording.stop())!
    expect(result.pointers.map((point) => point.event)).toEqual(['move', 'down', 'up', 'move', 'leave'])
    expect(result.pointers[0]!.t_ms).toBe(100)
    expect(result.pointers[1]!.t_ms).toBe(VOICE_MAX_DURATION_MS - 100)
    expect(result.pointers[3]!.target?.path).toBe('/workspace/other.png')
    expect(pointingError.value).toBeNull()
    expect(transport.send).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('aborts an in-flight capture on stop without corrupting the accepted recording', async () => {
    const { iframe, send } = viewer()
    unregister = registerPointingDesktop('workspace-a', () => iframe)
    recording = startPointing('workspace-a', recordingId, performance.now())
    await vi.advanceTimersByTimeAsync(0)
    pointAt(iframe)
    frameReply(iframe, frameRequests(send)[0]!.requestId)
    await vi.advanceTimersByTimeAsync(500)
    expect(frameRequests(send)).toHaveLength(2)
    const result = (await recording.stop())!
    // A late encoder callback from the disabled observer must have no effect.
    frameReply(iframe, frameRequests(send)[1]!.requestId, { data: {} })
    expect(result.frames).toHaveLength(1)
    expect(pointingError.value).toBeNull()
    expect(transport.send).toHaveBeenCalledOnce()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('forces a fresh baseline when the observed desktop viewer reconnects', async () => {
    const { iframe, send } = viewer()
    unregister = registerPointingDesktop('workspace-a', () => iframe)
    recording = startPointing('workspace-a', recordingId, performance.now())
    await vi.advanceTimersByTimeAsync(0)
    pointAt(iframe)
    frameReply(iframe, frameRequests(send)[0]!.requestId)
    await vi.advanceTimersByTimeAsync(100)
    iframe.removeAttribute('data-voice-desktop')
    await vi.advanceTimersByTimeAsync(500)
    iframe.dataset.voiceDesktop = ''
    await vi.advanceTimersByTimeAsync(500)
    expect(frameRequests(send)).toHaveLength(2)
    expect(frameRequests(send)[1]!.previousFrameId).toBeUndefined()
    frameReply(iframe, frameRequests(send)[1]!.requestId)
    await vi.advanceTimersByTimeAsync(0)
    expect((await recording.stop())!.frames).toHaveLength(2)
  })

  it('still reports failed uploads of accepted frames when stopping', async () => {
    const { iframe, send } = viewer()
    let rejectUpload!: (error: Error) => void
    transport.track.mockImplementationOnce(
      () =>
        new Promise<void>((_, reject) => {
          rejectUpload = reject
        }),
    )
    unregister = registerPointingDesktop('workspace-a', () => iframe)
    recording = startPointing('workspace-a', recordingId, performance.now())
    await vi.advanceTimersByTimeAsync(0)
    pointAt(iframe)
    frameReply(iframe, frameRequests(send)[0]!.requestId)
    await vi.advanceTimersByTimeAsync(100)
    const stopped = recording.stop()
    const rejected = expect(stopped).rejects.toThrow('upload failed')
    rejectUpload(new Error('upload failed'))
    await rejected
    expect(vi.getTimerCount()).toBe(0)
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

function frameRequests(
  send: ReturnType<typeof vi.fn>,
): { requestId: string; previousFrameId?: string }[] {
  return send.mock.calls
    .map((call) => call[0])
    .filter((data) => data.type === 'opencuria:voice-frame')
}
