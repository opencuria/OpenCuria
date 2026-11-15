/** Shared plain-text clipboard operations. Never log or toast copied content. */
import { useNotificationStore } from '@/stores/notifications'
import { useClipboardStore } from '@/stores/clipboard'

import { copyWithExecCommand, writeClipboardText } from './clipboardWrite'

export type ClipboardText = string | (() => Promise<string>)
export { writeClipboardText } from './clipboardWrite'

/**
 * Copy with common feedback and a selectable retry dialog when blocked.
 * A deferred producer reserves Safari's user gesture by supplying a promise
 * to ClipboardItem immediately, rather than awaiting the text before write().
 */
export async function copyToClipboard(
  source: ClipboardText,
  label: string,
  options: {
    success?: 'toast' | 'silent'
    signal?: AbortSignal
    context?: Window
    fallback?: 'dialog' | 'none'
  } = {},
): Promise<boolean> {
  const notifications = useNotificationStore()
  if (options.signal?.aborted) return false
  const context = (options.context ?? window) as Window & typeof globalThis
  const clipboard = context.navigator.clipboard
  const Item = context.ClipboardItem
  const allowDialog = options.fallback !== 'none'
  let ok = false
  try {
    if (typeof source === 'string') {
      ok = await writeClipboardText(source, options.signal, context)
      if (!ok && !options.signal?.aborted && allowDialog) {
        notifications.error(
          `Failed to copy ${label}`,
          'Use Copy now or select the text to copy manually.',
        )
        ok = await useClipboardStore().requestCopy(source, label, options.signal)
      }
    } else {
      // Invoke once, synchronously. Both the modern write and retry share it.
      const text = source().then((value) => {
        if (options.signal?.aborted) throw new Error('Copy cancelled')
        return value
      })
      // Handle a producer rejection even if ClipboardItem construction throws.
      void text.catch(() => {})
      if (typeof clipboard?.write === 'function' && typeof Item !== 'undefined') {
        const blob = text.then((value) => {
          if (options.signal?.aborted) throw new Error('Copy cancelled')
          return new context.Blob([value], { type: 'text/plain' })
        })
        void blob.catch(() => {})
        try {
          await clipboard.write([new Item({ 'text/plain': blob })])
          ok = true
        } catch {
          // Generic copy actions retain the fresh-click dialog fallback.
          // Desktop keyboard gestures can try the confirmed legacy path below.
        }
      }
      if (!ok && !options.signal?.aborted && !allowDialog) {
        // Legacy clipboard events may still be authorized by this keyboard
        // gesture on HTTP. Require confirmed payload delivery; never copy a
        // cached value or claim success if the browser blocks the command.
        const value = await text
        if (!options.signal?.aborted) ok = copyWithExecCommand(value, context.document)
      }
      if (!ok && !options.signal?.aborted && allowDialog) {
        ok = await useClipboardStore().requestCopy(text, label, options.signal)
      }
    }
  } catch {
    if (options.signal?.aborted) return false
    notifications.error(
      `Failed to copy ${label}`,
      'Clipboard content could not be loaded. Try again.',
    )
    return false
  }
  if (options.signal?.aborted) return false
  if (!ok && !allowDialog) {
    notifications.error(
      `Failed to copy ${label}`,
      context.isSecureContext === false
        ? 'Open OpenCuria over HTTPS to enable automatic desktop copying in Safari.'
        : 'Allow clipboard access for OpenCuria in your browser and try again.',
    )
  }
  if (ok && options.success !== 'silent') notifications.info(`Copied ${label}`)
  return ok
}
