/** Connection-state helpers for the embedded KasmVNC viewer. */
export {
  createDesktopReconnectBackoff,
  createPausableTimer,
  parseDesktopConnectionStatus,
} from '@opencuria/chat-core/desktopSurface'
export type {
  DesktopConnectionStatus,
  DesktopReconnectBackoff,
  PausableTimer,
} from '@opencuria/chat-core/desktopSurface'

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
