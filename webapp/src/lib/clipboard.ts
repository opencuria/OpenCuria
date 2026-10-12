/** Shared plain-text clipboard operations. Never log or toast copied content. */
import { useNotificationStore } from '@/stores/notifications'
import { useClipboardStore } from '@/stores/clipboard'

import { writeClipboardText } from './clipboardWrite'

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
  options: { success?: 'toast' | 'silent'; signal?: AbortSignal } = {},
): Promise<boolean> {
  const notifications = useNotificationStore()
  if (options.signal?.aborted) return false
  let ok = false
  try {
    if (typeof source === 'string') {
      ok = await writeClipboardText(source, options.signal)
      if (!ok && !options.signal?.aborted) {
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
      if (
        typeof navigator !== 'undefined' &&
        typeof navigator.clipboard?.write === 'function' &&
        typeof ClipboardItem !== 'undefined'
      ) {
        const blob = text.then((value) => {
          if (options.signal?.aborted) throw new Error('Copy cancelled')
          return new Blob([value], { type: 'text/plain' })
        })
        void blob.catch(() => {})
        try {
          await navigator.clipboard.write([new ClipboardItem({ 'text/plain': blob })])
          ok = true
        } catch {
          // Do not attempt execCommand after fetching: it needs a fresh click.
        }
      }
      if (!ok && !options.signal?.aborted) {
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
  if (ok && options.success !== 'silent') notifications.info(`Copied ${label}`)
  return ok
}
