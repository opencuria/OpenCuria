/**
 * Notification / toast store.
 *
 * Facade over vue-sonner so consuming stores keep a stable API.
 */

import { defineStore } from 'pinia'
import { toast } from 'vue-sonner'

export type NotificationType = 'success' | 'error' | 'warning' | 'info'

/** Inline toast button, e.g. Undo. */
export interface NotificationAction {
  label: string
  onClick: () => void
}

export const useNotificationStore = defineStore('notifications', () => {
  function add(
    type: NotificationType,
    title: string,
    message?: string,
    duration = 5000,
    action?: NotificationAction,
  ): void {
    const options = {
      ...(message ? { description: message } : {}),
      duration,
      ...(action ? { action } : {}),
    }

    switch (type) {
      case 'success':
        toast.success(title, options)
        break
      case 'error':
        toast.error(title, options)
        break
      case 'warning':
        toast.warning(title, options)
        break
      case 'info':
        toast.info(title, options)
        break
    }
  }

  function success(title: string, message?: string, action?: NotificationAction): void {
    add('success', title, message, action ? 8000 : 5000, action)
  }

  function error(title: string, message?: string): void {
    add('error', title, message, 8000)
  }

  function warning(title: string, message?: string): void {
    add('warning', title, message)
  }

  function info(title: string, message?: string): void {
    add('info', title, message)
  }

  return { add, success, error, warning, info }
})
