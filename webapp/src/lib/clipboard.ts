/**
 * Copy text to the clipboard without ever producing an unhandled promise
 * rejection (e.g. missing Clipboard API in Safari / insecure HTTP, or
 * denied permissions in headless environments).
 *
 * `writeClipboardText` is toast-free. `copyToClipboard` reports success
 * and failure through the notification store. Both return true on success.
 */

import { useNotificationStore } from '@/stores/notifications'

/**
 * Write plain text to the clipboard. Tries the Clipboard API first, then
 * falls back to a hidden textarea + `document.execCommand('copy')`.
 */
export async function writeClipboardText(text: string): Promise<boolean> {
  const writeText = navigator.clipboard?.writeText
  if (typeof writeText === 'function') {
    try {
      await writeText.call(navigator.clipboard, text)
      return true
    } catch {
      // Fall through to execCommand (Safari / insecure context / denied).
    }
  }
  return copyWithExecCommand(text)
}

/**
 * Copy text and toast the result. Success uses the existing
 * `Copied ${label}` info toast; both write paths failing use
 * `Failed to copy ${label}`.
 */
export async function copyToClipboard(text: string, label: string): Promise<boolean> {
  const notifications = useNotificationStore()
  const ok = await writeClipboardText(text)
  if (ok) {
    notifications.info(`Copied ${label}`, text)
    return true
  }
  notifications.error(`Failed to copy ${label}`, 'Clipboard unavailable')
  return false
}

function copyWithExecCommand(text: string): boolean {
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'fixed'
  textarea.style.top = '0'
  textarea.style.left = '0'
  textarea.style.width = '1px'
  textarea.style.height = '1px'
  textarea.style.padding = '0'
  textarea.style.border = 'none'
  textarea.style.outline = 'none'
  textarea.style.boxShadow = 'none'
  textarea.style.background = 'transparent'
  textarea.style.opacity = '0'
  document.body.appendChild(textarea)
  textarea.focus()
  textarea.select()
  textarea.setSelectionRange(0, text.length)
  let ok = false
  try {
    ok = typeof document.execCommand === 'function' && document.execCommand('copy')
  } catch {
    ok = false
  } finally {
    textarea.remove()
  }
  return ok
}
