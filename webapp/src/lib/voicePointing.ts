/**
 * Synchronized, workspace-scoped visual references for a voice recording.
 *
 * Observation is passive and starts with every dictation; it only produces a
 * result once the speaker actually points at a workspace image or desktop.
 * Desktop frames are captured lazily from the first pointer on the desktop.
 */
import { shallowRef } from 'vue'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import { sendFilesUpload } from '@/services/socket'
import { fileToBase64 } from '@/lib/chatUpload'
import { isTrustedDesktopMessage } from '@/lib/desktopSurfaceRecovery'
import { createSecureUuid } from '@/lib/secureUuid'

export interface PointerTarget {
  kind: 'image' | 'desktop'
  path: string
  width: number
  height: number
}

export interface PointerSample {
  t_ms: number
  event: 'move' | 'down' | 'up' | 'leave'
  x: number | null
  y: number | null
  target: PointerTarget | null
}

export interface PointingResult {
  pointers: PointerSample[]
  frames: { t_ms: number; path: string }[]
}

export const activePointingWorkspace = shallowRef<string | null>(null)
export const pointingError = shallowRef<string | null>(null)
/** True once the active recording has linked a pointer or frame to an image/desktop. */
export const pointingEngaged = shallowRef(false)
const desktops = new Map<string, () => HTMLIFrameElement | null>()
let activeOwner: symbol | null = null
const MAX_DURATION_MS = 5 * 60 * 1000
const MAX_FRAME_BYTES = 2 * 1024 * 1024
const MAX_TOTAL_BYTES = 64 * 1024 * 1024
const FRAME_INTERVAL_MS = 500
const MAX_FRAME_INTERVAL_MS = 30_000
const IDLE_BACKOFF_STEP_MS = 5000
// The centered hotspot keeps recorded coordinates at the laser's bright core.
const LASER_CURSOR = `url("data:image/svg+xml,${encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">' +
    '<circle cx="12" cy="12" r="10" fill="#ef4444" opacity=".18"/>' +
    '<circle cx="12" cy="12" r="6" fill="#ef4444" opacity=".35"/>' +
    '<circle cx="12" cy="12" r="3.5" fill="#ff2424" stroke="#fff" stroke-width="1"/>' +
    '</svg>',
)}") 12 12, crosshair`

/** Register the persistent viewer without exposing its authentication URL in logs. */
export function registerPointingDesktop(
  workspaceId: string,
  iframe: () => HTMLIFrameElement | null,
): () => void {
  desktops.set(workspaceId, iframe)
  return () => {
    if (desktops.get(workspaceId) === iframe) desktops.delete(workspaceId)
  }
}

/** Map the rendered image rectangle (including transforms) to natural pixels. */
export function imagePoint(
  rect: Pick<DOMRect, 'left' | 'top' | 'width' | 'height'>,
  width: number,
  height: number,
  clientX: number,
  clientY: number,
): { x: number; y: number } | null {
  if (rect.width <= 0 || rect.height <= 0 || width <= 0 || height <= 0) return null
  const x = (clientX - rect.left) / rect.width
  const y = (clientY - rect.top) / rect.height
  if (x < 0 || x >= 1 || y < 0 || y >= 1) return null
  return {
    x: Math.min(width - 1, Math.floor(x * width)),
    y: Math.min(height - 1, Math.floor(y * height)),
  }
}

function workspaceImagePath(path: string | undefined): path is string {
  return (
    !!path &&
    path.startsWith('/workspace/') &&
    !/[\\\u0000-\u001f]/.test(path) &&
    !path.split('/').some((part, index) => index > 0 && ['', '.', '..'].includes(part))
  )
}

export interface DesktopFrame {
  blob: Blob
  width: number
  height: number
  capturedAt: number
  frameId: string
}

interface UnchangedDesktopFrame {
  unchanged: true
  capturedAt: number
}

/** Request changed pixels as JPEG, accepting only this viewer, origin and request. */
export function captureDesktopFrame(
  iframe: HTMLIFrameElement,
  signal: AbortSignal,
  previousFrameId?: string,
): Promise<DesktopFrame | UnchangedDesktopFrame> {
  return new Promise((resolve, reject) => {
    const requestId = createSecureUuid()
    if (!requestId) {
      reject(new Error('Secure desktop capture IDs are unavailable in this browser.'))
      return
    }
    const finish = (error?: Error, frame?: DesktopFrame | UnchangedDesktopFrame): void => {
      clearTimeout(timer)
      window.removeEventListener('message', onMessage)
      signal.removeEventListener('abort', onAbort)
      if (error) reject(error)
      else resolve(frame!)
    }
    const onAbort = (): void => finish(new Error('Desktop capture cancelled.'))
    const onMessage = (event: MessageEvent): void => {
      if (!isTrustedDesktopMessage(event, iframe, window.location.href)) return
      const data = event.data
      if (data?.type !== 'opencuria:voice-frame' || data.requestId !== requestId) return
      if (data.unchanged === true) {
        if (!previousFrameId || !Number.isFinite(data.captured_at))
          finish(new Error('The desktop frame could not be captured.'))
        else finish(undefined, { unchanged: true, capturedAt: data.captured_at })
        return
      }
      if (
        typeof data.frameId !== 'string' ||
        !data.frameId ||
        data.frameId.length > 80 ||
        !(data.blob instanceof Blob) ||
        data.blob.type !== 'image/jpeg' ||
        data.blob.size === 0 ||
        data.blob.size > MAX_FRAME_BYTES ||
        !Number.isInteger(data.width) ||
        data.width < 1 ||
        data.width > 32768 ||
        !Number.isInteger(data.height) ||
        data.height < 1 ||
        data.height > 32768 ||
        !Number.isFinite(data.captured_at)
      ) {
        finish(new Error('The desktop frame could not be captured.'))
      } else
        finish(undefined, {
          frameId: data.frameId,
          blob: data.blob,
          width: data.width,
          height: data.height,
          capturedAt: data.captured_at,
        })
    }
    const timer = setTimeout(
      () => finish(new Error('Desktop capture timed out. Reopen the desktop and retry.')),
      5000,
    )
    window.addEventListener('message', onMessage)
    signal.addEventListener('abort', onAbort, { once: true })
    if (signal.aborted) {
      onAbort()
      return
    }
    try {
      const origin = new URL(iframe.src, window.location.href).origin
      if (!iframe.contentWindow) throw new Error('Desktop viewer is unavailable.')
      iframe.contentWindow.postMessage(
        { type: 'opencuria:voice-frame', requestId, previousFrameId },
        origin,
      )
    } catch {
      finish(new Error('Desktop viewer is unavailable.'))
    }
  })
}

/**
 * Observe image and iframe pointers without intercepting desktop interaction.
 * `stop()` resolves to `null` when nothing was pointed at during the recording.
 */
export function startPointing(
  workspaceId: string,
  recordingId: string,
  originMs: number,
): {
  stop(): Promise<PointingResult | null>
  cancel(): void
  audioActivity(): void
} {
  if (activePointingWorkspace.value) throw new Error('A pointing recording is already active.')
  if (!workspaceId || !/^[0-9a-f-]{36}$/i.test(recordingId)) throw new Error('Invalid recording.')
  const owner = Symbol(recordingId)
  activeOwner = owner
  pointingError.value = null
  pointingEngaged.value = false
  activePointingWorkspace.value = workspaceId
  const pointers: PointerSample[] = []
  const frames: PointingResult['frames'] = []
  const frameSizes = new Map<string, { width: number; height: number }>()
  const abort = new AbortController()
  let lastPoint: { x: number; y: number } | null = null
  let desktopPoint: { x: number; y: number; width: number; height: number } | null = null
  let observedViewer: HTMLIFrameElement | null = null
  let stopped = false
  let cancelled = false
  let engaged = false
  // Desktop frames are only worth capturing once the speaker points at the desktop.
  let desktopRequested = false
  let failure: Error | null = null
  let pendingCapture: Promise<void> | null = null
  const pendingUploads = new Set<Promise<void>>()
  const uploadIds = new Set<string>()
  let totalBytes = 0
  let lastActivityMs = originMs
  let nextCaptureMs = -Infinity
  let previousFrameId: string | undefined
  let lastDesktopPosition: { x: number; y: number } | null = null
  const activity = (): void => {
    if (stopped || failure) return
    lastActivityMs = performance.now()
    nextCaptureMs = Math.min(nextCaptureMs, lastActivityMs + FRAME_INTERVAL_MS)
  }
  let cursorImage: HTMLImageElement | null = null
  const cursorStyle = document.createElement('style')
  cursorStyle.textContent = `[data-voice-pointing-cursor] { cursor: ${LASER_CURSOR} !important; }`
  document.head.append(cursorStyle)
  const showImageCursor = (image: HTMLImageElement | null): void => {
    if (cursorImage === image) return
    cursorImage?.removeAttribute('data-voice-pointing-cursor')
    cursorImage = image
    cursorImage?.setAttribute('data-voice-pointing-cursor', '')
  }
  const originEpoch = performance.timeOrigin + originMs
  const elapsed = (): number => Math.max(0, Math.round(performance.now() - originMs))
  const engage = (): void => {
    if (engaged) return
    engaged = true
    if (activeOwner === owner) pointingEngaged.value = true
  }
  /** The speaker referenced an image or the desktop, even if its capture failed. */
  const visual = (): boolean => engaged || desktopRequested
  const fail = (error: Error): void => {
    failure = error
    showImageCursor(null)
    if (observedViewer) observe(observedViewer, false)
    if (activeOwner === owner && visual()) pointingError.value = error.message
  }
  const observe = (iframe: HTMLIFrameElement, enabled: boolean): void => {
    try {
      const size = frameSizes.get(frames[frames.length - 1]?.path ?? '')
      iframe.contentWindow?.postMessage(
        {
          type: 'opencuria:voice-observe',
          sessionId: recordingId,
          enabled,
          cursor: LASER_CURSOR,
          frameWidth: size?.width ?? 0,
          frameHeight: size?.height ?? 0,
        },
        new URL(iframe.src, window.location.href).origin,
      )
    } catch {
      /* A navigating viewer will be retried on the next observation. */
    }
  }
  const visibleViewer = (): HTMLIFrameElement | null => {
    const iframe = desktops.get(workspaceId)?.()
    if (
      !iframe ||
      !iframe.hasAttribute('data-voice-desktop') ||
      iframe.dataset.voiceWorkspace !== workspaceId
    )
      return null
    const rect = iframe.getBoundingClientRect()
    const style = getComputedStyle(iframe)
    return rect.width > 1 &&
      rect.height > 1 &&
      style.visibility !== 'hidden' &&
      style.display !== 'none'
      ? iframe
      : null
  }
  const syncViewer = (): HTMLIFrameElement | null => {
    const iframe = visibleViewer()
    if (observedViewer && observedViewer !== iframe) {
      observe(observedViewer, false)
      desktopPoint = null
      lastDesktopPosition = null
      previousFrameId = undefined
      nextCaptureMs = -Infinity
    }
    observedViewer = iframe
    if (iframe) observe(iframe, true)
    return iframe
  }
  const resolvePoint = (t_ms: number): Omit<PointerSample, 'event' | 't_ms'> => {
    const empty = (): Omit<PointerSample, 'event' | 't_ms'> => {
      showImageCursor(null)
      return { x: null, y: null, target: null }
    }
    if (stopped || failure || t_ms > MAX_DURATION_MS || document.visibilityState === 'hidden')
      return empty()
    if (desktopPoint && observedViewer) {
      showImageCursor(null)
      const frame = [...frames].reverse().find((item) => item.t_ms <= t_ms)
      const size = frame && frameSizes.get(frame.path)
      if (frame && size?.width === desktopPoint.width && size.height === desktopPoint.height)
        return {
          x: desktopPoint.x,
          y: desktopPoint.y,
          target: {
            kind: 'desktop',
            path: frame.path,
            width: desktopPoint.width,
            height: desktopPoint.height,
          },
        }
      return empty()
    }
    if (!lastPoint) return empty()
    const element = document.elementFromPoint(lastPoint.x, lastPoint.y)
    const image = element?.closest<HTMLImageElement>('img[data-voice-image-path]')
    if (
      image &&
      image.dataset.voiceWorkspace === workspaceId &&
      workspaceImagePath(image.dataset.voiceImagePath)
    ) {
      const point = imagePoint(
        image.getBoundingClientRect(),
        image.naturalWidth,
        image.naturalHeight,
        lastPoint.x,
        lastPoint.y,
      )
      if (point) {
        showImageCursor(image)
        return {
          ...point,
          target: {
            kind: 'image',
            path: image.dataset.voiceImagePath,
            width: image.naturalWidth,
            height: image.naturalHeight,
          },
        }
      }
    }
    return empty()
  }
  const sample = (event: PointerSample['event'], t_ms = elapsed()): void => {
    if (stopped || failure) return
    if (t_ms > MAX_DURATION_MS) {
      showImageCursor(null)
      if (observedViewer) observe(observedViewer, false)
      return
    }
    if (pointers.length >= 6000) {
      fail(new Error('The pointing recording reached its event limit.'))
      return
    }
    const point = resolvePoint(t_ms)
    if (point.target) engage()
    pointers.push({ t_ms, event, ...point })
  }
  const pointer = (event: PointerEvent): void => {
    if (
      event.type !== 'pointermove' ||
      !lastPoint ||
      lastPoint.x !== event.clientX ||
      lastPoint.y !== event.clientY
    )
      activity()
    desktopPoint = null
    lastPoint = { x: event.clientX, y: event.clientY }
    if (event.type === 'pointerdown') sample('down')
    else if (event.type === 'pointerup') sample('up')
    else resolvePoint(elapsed())
  }
  const leave = (): void => {
    lastPoint = null
    desktopPoint = null
    lastDesktopPosition = null
    showImageCursor(null)
    sample('leave')
  }
  const onOut = (event: PointerEvent): void => {
    if (!event.relatedTarget) leave()
  }
  const onBlur = (): void => {
    // Focusing an iframe blurs the parent window without leaving the desktop.
    if (document.activeElement !== observedViewer) leave()
  }
  const visibility = (): void => {
    if (document.visibilityState === 'hidden') leave()
  }
  const desktopPointer = (event: MessageEvent): void => {
    const iframe = observedViewer
    if (!isTrustedDesktopMessage(event, iframe, window.location.href)) return
    const data = event.data
    if (
      data?.type !== 'opencuria:voice-pointer' ||
      data.sessionId !== recordingId ||
      !Number.isFinite(data.observed_at)
    )
      return
    const t_ms = Math.round(data.observed_at - originEpoch)
    if (t_ms < 0 || t_ms > elapsed() + 100 || !['move', 'down', 'up', 'leave'].includes(data.event))
      return
    if (data.event === 'leave') {
      leave()
      return
    }
    if (
      !Number.isInteger(data.width) ||
      data.width < 1 ||
      data.width > 32768 ||
      !Number.isInteger(data.height) ||
      data.height < 1 ||
      data.height > 32768 ||
      !Number.isFinite(data.x) ||
      data.x < 0 ||
      data.x >= data.width ||
      !Number.isFinite(data.y) ||
      data.y < 0 ||
      data.y >= data.height
    )
      return
    // Viewer heartbeats repeat the same point even when the mouse is still.
    if (
      data.event !== 'move' ||
      !lastDesktopPosition ||
      lastDesktopPosition.x !== data.x ||
      lastDesktopPosition.y !== data.y
    )
      activity()
    lastDesktopPosition = { x: data.x, y: data.y }
    lastPoint = null
    desktopPoint = { x: data.x, y: data.y, width: data.width, height: data.height }
    // The parent supplies regular 100 ms move samples; retain discrete clicks.
    if (data.event !== 'move') sample(data.event, t_ms)
    if (!desktopRequested) {
      desktopRequested = true
      capture()
    }
  }
  /** Renew viewer observation; capture frames only after the desktop was pointed at. */
  const capture = (): void => {
    if (stopped || failure || document.visibilityState === 'hidden' || elapsed() > MAX_DURATION_MS)
      return
    const iframe = syncViewer()
    if (!desktopRequested || pendingCapture || !iframe) return
    // Observation stays live at 500 ms, independently of the capture cadence.
    // Every five seconds without speech or mouse activity doubles the interval.
    const idleSteps = Math.floor((performance.now() - lastActivityMs) / IDLE_BACKOFF_STEP_MS)
    const interval = Math.min(MAX_FRAME_INTERVAL_MS, FRAME_INTERVAL_MS * 2 ** idleSteps)
    if (performance.now() < nextCaptureMs) return
    nextCaptureMs = performance.now() + interval
    pendingCapture = (async () => {
      const frame = await captureDesktopFrame(iframe, abort.signal, previousFrameId)
      const t_ms = Math.round(frame.capturedAt - originEpoch)
      if (stopped || cancelled || iframe !== observedViewer || t_ms > MAX_DURATION_MS) return
      if (t_ms < 0 || t_ms > elapsed() + 100)
        throw new Error('The desktop returned an invalid frame timestamp.')
      if ('unchanged' in frame) return
      if (pendingUploads.size >= 8)
        throw new Error('Desktop uploads are too slow to keep recording.')
      totalBytes += frame.blob.size
      if (totalBytes > MAX_TOTAL_BYTES)
        throw new Error('The visual recording reached its size limit.')
      const directory = `/workspace/.opencuria/recordings/${recordingId}/frames`
      const index = frames.length
      const filename = `${String(index).padStart(6, '0')}.jpg`
      const content = await fileToBase64(new File([frame.blob], filename, { type: 'image/jpeg' }))
      if (stopped || cancelled) return
      const path = `${directory}/${filename}`
      frameSizes.set(path, { width: frame.width, height: frame.height })
      frames.push({ t_ms, path })
      previousFrameId = frame.frameId
      engage()
      observe(iframe, !failure)
      const store = useFileExplorerStore()
      const uploadId = `voice-frame-${recordingId}-${index}`
      uploadIds.add(uploadId)
      const tracked = store.trackAndUpload(
        workspaceId,
        uploadId,
        directory,
        filename,
        content,
        false,
      )
      const upload = tracked
        .catch((error: unknown) => {
          if (!cancelled)
            fail(error instanceof Error ? error : new Error('Desktop recording upload failed.'))
        })
        .finally(() => {
          uploadIds.delete(uploadId)
          pendingUploads.delete(upload)
        })
      pendingUploads.add(upload)
      try {
        sendFilesUpload(workspaceId, uploadId, directory, filename, content, false)
      } catch {
        store.failUpload(uploadId, 'The desktop frame could not be saved.')
      }
    })()
      .catch((error: unknown) => {
        if (!cancelled && !stopped)
          fail(error instanceof Error ? error : new Error('Desktop recording failed.'))
      })
      .finally(() => {
        pendingCapture = null
      })
  }
  const sampleTimer = setInterval(() => sample('move'), 100)
  const frameTimer = setInterval(capture, FRAME_INTERVAL_MS)
  const listeners = ['pointermove', 'pointerdown', 'pointerup'] as const
  for (const name of listeners) document.addEventListener(name, pointer, true)
  document.addEventListener('pointerout', onOut, true)
  window.addEventListener('blur', onBlur)
  window.addEventListener('message', desktopPointer)
  document.addEventListener('visibilitychange', visibility)
  queueMicrotask(capture)

  const cleanup = (): void => {
    stopped = true
    abort.abort()
    showImageCursor(null)
    cursorStyle.remove()
    clearInterval(sampleTimer)
    clearInterval(frameTimer)
    for (const name of listeners) document.removeEventListener(name, pointer, true)
    document.removeEventListener('pointerout', onOut, true)
    window.removeEventListener('blur', onBlur)
    window.removeEventListener('message', desktopPointer)
    document.removeEventListener('visibilitychange', visibility)
    if (observedViewer) observe(observedViewer, false)
    observedViewer = null
    if (activeOwner === owner) {
      activePointingWorkspace.value = null
      activeOwner = null
    }
  }
  return {
    audioActivity: activity,
    async stop() {
      if (!stopped) leave()
      cleanup()
      await pendingCapture
      await Promise.all(pendingUploads)
      if (cancelled) throw new Error('Pointing recording cancelled.')
      if (failure && visual()) throw failure
      // Plain dictation: nothing was pointed at, so nothing visual is attached.
      if (!engaged) return null
      pointers.sort((a, b) => a.t_ms - b.t_ms)
      return { pointers, frames }
    },
    cancel() {
      cancelled = true
      cleanup()
      abort.abort()
      for (const uploadId of uploadIds) useFileExplorerStore().cancelUpload(uploadId)
    },
  }
}
