/** Connection-state helpers for the embedded KasmVNC viewer. */
export type DesktopConnectionStatus = 'connected' | 'disconnected' | 'connecting'

const DESKTOP_CONNECTION_STATUSES = new Set<DesktopConnectionStatus>([
  'connected',
  'disconnected',
  'connecting',
])

export function parseDesktopConnectionStatus(value: unknown): DesktopConnectionStatus | null {
  if (!value || typeof value !== 'object') return null
  const message = value as { action?: unknown; value?: unknown }
  if (message.action !== 'connection_state') return null
  return typeof message.value === 'string' &&
    DESKTOP_CONNECTION_STATUSES.has(message.value as DesktopConnectionStatus)
    ? (message.value as DesktopConnectionStatus)
    : null
}

/** Reject statuses from other frames and messages from another origin. */
export function isTrustedDesktopMessage(
  event: Pick<MessageEvent, 'origin' | 'source'>,
  iframe: HTMLIFrameElement | null,
  pageUrl: string,
): boolean {
  if (!iframe?.contentWindow || event.source !== iframe.contentWindow || !iframe.src) return false
  try {
    return event.origin === new URL(iframe.src, pageUrl).origin
  } catch {
    return false
  }
}

export type NativeClipboardFallbackReason = 'unsupported' | 'permission' | 'unavailable'
export type NativeClipboardMessage =
  | { kind: 'ready' }
  | { kind: 'focus'; focused: boolean }
  | { kind: 'fallback'; reason: NativeClipboardFallbackReason }

export const NATIVE_CLIPBOARD_ACTION = 'opencuria_clipboard'
export const NATIVE_CLIPBOARD_VERSION = 1

export interface NativeClipboardContext {
  action: typeof NATIVE_CLIPBOARD_ACTION
  version: typeof NATIVE_CLIPBOARD_VERSION
  kind: 'context'
  sequence: number
  workspaceId: string
  enabled: boolean
  connected: boolean
  visible: boolean
  parentFocused: boolean
  computerUseActive: boolean
}

/** Construct the minimized, typed context sent to the trusted Kasm frame. */
export function createNativeClipboardContext(
  sequence: number,
  workspaceId: string,
  values: Omit<NativeClipboardContext, 'action' | 'version' | 'kind' | 'sequence' | 'workspaceId'>,
): NativeClipboardContext {
  return {
    action: NATIVE_CLIPBOARD_ACTION,
    version: NATIVE_CLIPBOARD_VERSION,
    kind: 'context',
    sequence,
    workspaceId,
    ...values,
  }
}

/** Parse only versioned, non-secret status messages from the Kasm bridge. */
export function parseNativeClipboardMessage(value: unknown): NativeClipboardMessage | null {
  if (!value || typeof value !== 'object') return null
  const message = value as Record<string, unknown>
  if (message.action !== NATIVE_CLIPBOARD_ACTION || message.version !== NATIVE_CLIPBOARD_VERSION)
    return null
  if (message.kind === 'ready') return { kind: 'ready' }
  if (message.kind === 'focus' && typeof message.focused === 'boolean')
    return { kind: 'focus', focused: message.focused }
  if (
    message.kind === 'fallback' &&
    (message.reason === 'unsupported' ||
      message.reason === 'permission' ||
      message.reason === 'unavailable')
  )
    return { kind: 'fallback', reason: message.reason }
  return null
}

export interface PausableTimer {
  start(delayMs: number): void
  pause(): void
  resume(): void
  cancel(): void
  readonly pending: boolean
}

/** A one-shot timer whose remaining duration is retained while the tab is hidden. */
export function createPausableTimer(callback: () => void): PausableTimer {
  let handle: ReturnType<typeof setTimeout> | null = null
  let remainingMs: number | null = null
  let startedAt = 0

  const schedule = (delayMs: number): void => {
    remainingMs = Math.max(0, delayMs)
    startedAt = Date.now()
    handle = setTimeout(() => {
      handle = null
      remainingMs = null
      callback()
    }, remainingMs)
  }

  return {
    start(delayMs) {
      this.cancel()
      schedule(delayMs)
    },
    pause() {
      if (handle === null || remainingMs === null) return
      clearTimeout(handle)
      remainingMs = Math.max(0, remainingMs - (Date.now() - startedAt))
      handle = null
    },
    resume() {
      if (handle !== null || remainingMs === null) return
      schedule(remainingMs)
    },
    cancel() {
      if (handle !== null) clearTimeout(handle)
      handle = null
      remainingMs = null
    },
    get pending() {
      return handle !== null || remainingMs !== null
    },
  }
}

export interface DesktopReconnectBackoff {
  schedule(): void
  pause(): void
  resume(): void
  reset(): void
  retryNow(): void
  readonly attempts: number
}

/** Retry a lost viewer up to three times with capped exponential backoff. */
export function createDesktopReconnectBackoff(
  onRetry: () => void,
  onExhausted: () => void,
): DesktopReconnectBackoff {
  const delays = [1_000, 2_000, 4_000]
  let attempts = 0
  const timer = createPausableTimer(onRetry)

  return {
    schedule() {
      if (timer.pending) return
      if (attempts >= delays.length) {
        onExhausted()
        return
      }
      timer.start(delays[attempts] ?? delays[delays.length - 1]!)
      attempts += 1
    },
    pause: () => timer.pause(),
    resume: () => timer.resume(),
    reset() {
      timer.cancel()
      attempts = 0
    },
    retryNow() {
      timer.cancel()
      attempts = 0
      onRetry()
    },
    get attempts() {
      return attempts
    },
  }
}
