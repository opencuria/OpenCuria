/**
 * Copy text to the clipboard without ever producing an unhandled promise
 * rejection (e.g. missing permissions in headless environments).
 *
 * Success triggers an info toast, failures an error toast through the
 * notification store. Returns true on success.
 */

import { useNotificationStore } from '@/stores/notifications'

export async function copyToClipboard(text: string, label: string): Promise<boolean> {
  const notifications = useNotificationStore()
  try {
    await navigator.clipboard.writeText(text)
    notifications.info(`Copied ${label}`, text)
    return true
  } catch (e: unknown) {
    const message = e instanceof Error && e.message ? e.message : 'Unknown error'
    notifications.error(`Failed to copy ${label}`, message)
    return false
  }
}
