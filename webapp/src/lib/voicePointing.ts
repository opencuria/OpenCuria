/**
 * Synchronized, workspace-scoped visual references for a voice recording.
 *
 * Observation is passive and starts with every dictation; it only produces a
 * result once the speaker actually points at a workspace image or desktop.
 * Desktop frames are captured lazily from the first pointer on the desktop.
 */
import { shallowRef } from 'vue'
import { workspaceFiles } from '@/services/workspaceFiles'
import { isTrustedDesktopMessage } from '@/lib/desktopSurfaceRecovery'
import { createSecureUuid } from '@/lib/secureUuid'
import { VOICE_MAX_DURATION_MS } from '@opencuria/chat-core/voiceInput'
import {
  FrameCadence,
  PointerTimeline,
  VISUAL_FRAME_INTERVAL_MS,
  VISUAL_MAX_FRAME_BYTES,
  VISUAL_MAX_PENDING_UPLOADS,
  VISUAL_MAX_TOTAL_BYTES,
  VISUAL_SAMPLE_INTERVAL_MS,
  frameAt,
  imagePixel,
  isWorkspacePath,
  recordingFrameName,
  recordingFramesDirectory,
  type PointerSample,
  type PointingResult,
} from '@opencuria/chat-core/visualRecording'

export const activePointingWorkspace = shallowRef<string | null>(null)
export const pointingError = shallowRef<string | null>(null)
/** True once the active recording has linked a pointer or frame to an image/desktop. */
export const pointingEngaged = shallowRef(false)
const desktops = new Map<string, () => HTMLIFrameElement | null>()
let activeOwner: symbol | null = null
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
  return imagePixel(
    { x: clientX, y: clientY },
    { x: rect.left, y: rect.top, width: rect.width, height: rect.height },
    { width, height },
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
        data.blob.size > VISUAL_MAX_FRAME_BYTES ||
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
  const pointers = new PointerTimeline()
  const frames: PointingResult['frames'] = []
  const frameSizes = new Map<string, { width: number; height: number }>()
  const abort = new AbortController()
  // Stopping keeps in-flight frame uploads; only cancelling aborts them.
  const uploadAbort = new AbortController()
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
  let totalBytes = 0
  const cadence = new FrameCadence(originMs)
  let previousFrameId: string | undefined
  let lastDesktopPosition: { x: number; y: number } | null = null
  const activity = (): void => {
    if (stopped || failure) return
    cadence.activity(performance.now())
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
      cadence.reset()
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
    if (stopped || failure || t_ms > VOICE_MAX_DURATION_MS || document.visibilityState === 'hidden')
      return empty()
    if (desktopPoint && observedViewer) {
      showImageCursor(null)
      const frame = frameAt(frames, t_ms)
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
      isWorkspacePath(image.dataset.voiceImagePath)
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
    if (t_ms > VOICE_MAX_DURATION_MS) {
      showImageCursor(null)
      if (observedViewer) observe(observedViewer, false)
      return
    }
    const point = resolvePoint(t_ms)
    if (point.target) engage()
    if (pointers.add({ t_ms, event, ...point }) === 'full')
      fail(new Error('The pointing recording reached its event limit.'))
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
    if (
      stopped ||
      failure ||
      document.visibilityState === 'hidden' ||
      elapsed() > VOICE_MAX_DURATION_MS
    )
      return
    const iframe = syncViewer()
    if (!desktopRequested || pendingCapture || !iframe) return
    // Observation stays live at 500 ms, independently of the capture cadence.
    // Every five seconds without speech or mouse activity doubles the interval.
    if (!cadence.due(performance.now())) return
    cadence.captured(performance.now())
    pendingCapture = (async () => {
      const frame = await captureDesktopFrame(iframe, abort.signal, previousFrameId)
      const t_ms = Math.round(frame.capturedAt - originEpoch)
      if (stopped || cancelled || iframe !== observedViewer || t_ms > VOICE_MAX_DURATION_MS) return
      if (t_ms < 0 || t_ms > elapsed() + 100)
        throw new Error('The desktop returned an invalid frame timestamp.')
      if ('unchanged' in frame) return
      if (pendingUploads.size >= VISUAL_MAX_PENDING_UPLOADS)
        throw new Error('Desktop uploads are too slow to keep recording.')
      totalBytes += frame.blob.size
      if (totalBytes > VISUAL_MAX_TOTAL_BYTES)
        throw new Error('The visual recording reached its size limit.')
      const directory = recordingFramesDirectory(recordingId)
      const filename = recordingFrameName(frames.length)
      const path = `${directory}/${filename}`
      frameSizes.set(path, { width: frame.width, height: frame.height })
      frames.push({ t_ms, path })
      previousFrameId = frame.frameId
      engage()
      observe(iframe, !failure)
      const upload = workspaceFiles()
        .upload(workspaceId, {
          directory,
          filename,
          body: frame.blob,
          conflict: 'replace',
          signal: uploadAbort.signal,
        })
        .then(
          () => undefined,
          (error: unknown) => {
            if (!cancelled)
              fail(error instanceof Error ? error : new Error('Desktop recording upload failed.'))
          },
        )
        .finally(() => {
          pendingUploads.delete(upload)
        })
      pendingUploads.add(upload)
    })()
      .catch((error: unknown) => {
        if (!cancelled && !stopped)
          fail(error instanceof Error ? error : new Error('Desktop recording failed.'))
      })
      .finally(() => {
        pendingCapture = null
      })
  }
  const sampleTimer = setInterval(() => sample('move'), VISUAL_SAMPLE_INTERVAL_MS)
  const frameTimer = setInterval(capture, VISUAL_FRAME_INTERVAL_MS)
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
      return { pointers: pointers.result(), frames }
    },
    cancel() {
      cancelled = true
      cleanup()
      abort.abort()
      uploadAbort.abort()
    },
  }
}
