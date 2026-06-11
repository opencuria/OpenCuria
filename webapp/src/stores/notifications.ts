/**
 * Notification / toast store.
 *
 * Facade over vue-sonner so consuming stores keep a stable API.
 */

import { defineStore } from 'pinia'
import { toast } from 'vue-sonner'

export type NotificationType = 'success' | 'error' | 'warning' | 'info'

export const useNotificationStore = defineStore('notifications', () => {
  function add(
    type: NotificationType,
    title: string,
    message?: string,
    duration = 5000,
  ): void {
    const options = message ? { description: message, duration } : { duration }

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

  function success(title: string, message?: string): void {
    add('success', title, message)
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
