/** Synchronized, workspace-scoped visual references for an explicit voice recording. */
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
const desktops = new Map<string, () => HTMLIFrameElement | null>()
let activeOwner: symbol | null = null
const MAX_DURATION_MS = 5 * 60 * 1000
const MAX_FRAME_BYTES = 2 * 1024 * 1024
const MAX_TOTAL_BYTES = 64 * 1024 * 1024

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

/** Request a JPEG from the authenticated viewer, accepting only this frame and origin. */
export function captureDesktopFrame(iframe: HTMLIFrameElement, signal: AbortSignal): Promise<Blob> {
  return new Promise((resolve, reject) => {
    const requestId = createSecureUuid()
    if (!requestId) {
      reject(new Error('Secure desktop capture IDs are unavailable in this browser.'))
      return
    }
    const finish = (error?: Error, blob?: Blob): void => {
      clearTimeout(timer)
      window.removeEventListener('message', onMessage)
      signal.removeEventListener('abort', onAbort)
      if (error) reject(error)
      else resolve(blob!)
    }
    const onAbort = (): void => finish(new Error('Desktop capture cancelled.'))
    const onMessage = (event: MessageEvent): void => {
      if (!isTrustedDesktopMessage(event, iframe, window.location.href)) return
      const data = event.data
      if (data?.type !== 'opencuria:voice-frame' || data.requestId !== requestId) return
      if (
        !(data.blob instanceof Blob) ||
        data.blob.type !== 'image/jpeg' ||
        data.blob.size === 0 ||
        data.blob.size > MAX_FRAME_BYTES
      ) {
        finish(new Error('The desktop frame could not be captured.'))
      } else finish(undefined, data.blob)
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
      iframe.contentWindow.postMessage({ type: 'opencuria:voice-frame', requestId }, origin)
    } catch {
      finish(new Error('Desktop viewer is unavailable.'))
    }
  })
}

/** Record only marked images and the desktop overlay; stop drains the single upload. */
export function startPointing(
  workspaceId: string,
  recordingId: string,
  originMs: number,
): {
  stop(): Promise<PointingResult>
  cancel(): void
} {
  if (activePointingWorkspace.value) throw new Error('A pointing recording is already active.')
  if (!workspaceId || !/^[0-9a-f-]{36}$/i.test(recordingId)) throw new Error('Invalid recording.')
  const owner = Symbol(recordingId)
  activeOwner = owner
  pointingError.value = null
  activePointingWorkspace.value = workspaceId
  const pointers: PointerSample[] = []
  const frames: PointingResult['frames'] = []
  const abort = new AbortController()
  let lastPoint: { x: number; y: number } | null = null
  let stopped = false
  let cancelled = false
  let failure: Error | null = null
  let pending: Promise<void> | null = null
  let uploadId: string | null = null
  let totalBytes = 0
  const fail = (error: Error): void => {
    failure = error
    if (activeOwner === owner) pointingError.value = error.message
  }
  const elapsed = (): number => Math.max(0, Math.round(performance.now() - originMs))

  const resolvePoint = (): Omit<PointerSample, 'event' | 't_ms'> => {
    const empty = { x: null, y: null, target: null }
    if (!lastPoint || document.visibilityState === 'hidden') return empty
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
      if (point)
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
    const desktop = element?.closest<HTMLElement>('[data-voice-desktop]')
    const frame = frames[frames.length - 1]
    if (desktop?.dataset.voiceWorkspace === workspaceId && frame) {
      const width = Number(desktop.dataset.voiceWidth)
      const height = Number(desktop.dataset.voiceHeight)
      const point = imagePoint(
        desktop.getBoundingClientRect(),
        width,
        height,
        lastPoint.x,
        lastPoint.y,
      )
      if (point) return { ...point, target: { kind: 'desktop', path: frame.path, width, height } }
    }
    return empty
  }
  const sample = (event: PointerSample['event']): void => {
    const t_ms = elapsed()
    if (stopped || t_ms > MAX_DURATION_MS) return
    if (pointers.length >= 6000) {
      fail(new Error('The pointing recording reached its event limit.'))
      return
    }
    pointers.push({ t_ms, event, ...resolvePoint() })
  }
  const pointer = (event: PointerEvent): void => {
    lastPoint = { x: event.clientX, y: event.clientY }
    if (event.type === 'pointerdown') sample('down')
    else if (event.type === 'pointerup') sample('up')
  }
  const leave = (): void => {
    lastPoint = null
    sample('leave')
  }
  const onOut = (event: PointerEvent): void => {
    if (!event.relatedTarget) leave()
  }
  const visibility = (): void => {
    if (document.visibilityState === 'hidden') leave()
  }

  const capture = (): void => {
    if (
      pending ||
      stopped ||
      failure ||
      document.visibilityState === 'hidden' ||
      elapsed() > MAX_DURATION_MS
    )
      return
    const overlay = Array.from(document.querySelectorAll<HTMLElement>('[data-voice-desktop]')).find(
      (entry) =>
        entry.dataset.voiceWorkspace === workspaceId &&
        entry.getBoundingClientRect().width > 1 &&
        entry.getBoundingClientRect().height > 1 &&
        getComputedStyle(entry).visibility !== 'hidden' &&
        getComputedStyle(entry).display !== 'none',
    )
    const iframe = desktops.get(workspaceId)?.()
    if (!overlay || !iframe) return
    pending = (async () => {
      const blob = await captureDesktopFrame(iframe, abort.signal)
      const t_ms = elapsed()
      if (stopped || cancelled || t_ms > MAX_DURATION_MS) return
      totalBytes += blob.size
      if (totalBytes > MAX_TOTAL_BYTES)
        throw new Error('The visual recording reached its size limit.')
      const directory = `/workspace/.opencuria/recordings/${recordingId}/frames`
      const filename = `${String(frames.length).padStart(6, '0')}.jpg`
      // Reference the frame as soon as it is observed, independently of upload
      // latency. stop() requires its upload to succeed before returning logs.
      frames.push({ t_ms, path: `${directory}/${filename}` })
      const content = await fileToBase64(new File([blob], filename, { type: 'image/jpeg' }))
      if (cancelled) return
      const store = useFileExplorerStore()
      uploadId = `voice-frame-${recordingId}-${frames.length}`
      const upload = store.trackAndUpload(
        workspaceId,
        uploadId,
        directory,
        filename,
        content,
        false,
      )
      try {
        sendFilesUpload(workspaceId, uploadId, directory, filename, content, false)
      } catch {
        store.failUpload(uploadId, 'The desktop frame could not be saved.')
      }
      await upload
      uploadId = null
    })()
      .catch((error: unknown) => {
        if (!cancelled)
          fail(error instanceof Error ? error : new Error('Desktop recording failed.'))
      })
      .finally(() => {
        pending = null
      })
  }
  const sampleTimer = setInterval(() => sample('move'), 100)
  const frameTimer = setInterval(capture, 500)
  const listeners = ['pointermove', 'pointerdown', 'pointerup'] as const
  for (const name of listeners) document.addEventListener(name, pointer, true)
  document.addEventListener('pointerout', onOut, true)
  window.addEventListener('blur', leave)
  document.addEventListener('visibilitychange', visibility)
  // The desktop overlay is reactive and mounts on the next Vue render.
  queueMicrotask(capture)

  const cleanup = (): void => {
    stopped = true
    clearInterval(sampleTimer)
    clearInterval(frameTimer)
    for (const name of listeners) document.removeEventListener(name, pointer, true)
    document.removeEventListener('pointerout', onOut, true)
    window.removeEventListener('blur', leave)
    document.removeEventListener('visibilitychange', visibility)
    if (activeOwner === owner) {
      activePointingWorkspace.value = null
      activeOwner = null
    }
  }
  return {
    async stop() {
      if (!stopped) leave()
      cleanup()
      await pending
      if (cancelled) throw new Error('Pointing recording cancelled.')
      if (failure) throw failure
      return { pointers, frames }
    },
    cancel() {
      cancelled = true
      cleanup()
      abort.abort()
      if (uploadId) useFileExplorerStore().cancelUpload(uploadId)
    },
  }
}
