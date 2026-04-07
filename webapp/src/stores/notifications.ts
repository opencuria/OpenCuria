/**
 * Notification / toast store.
 *
 * Provides a queue-based notification system for displaying
 * success, error, warning, and info toasts.
 */

import { defineStore } from 'pinia'
import { ref } from 'vue'

export type NotificationType = 'success' | 'error' | 'warning' | 'info'

export interface Notification {
  id: number
  type: NotificationType
  title: string
  message?: string
  duration: number
}

let nextId = 0

export const useNotificationStore = defineStore('notifications', () => {
  const notifications = ref<Notification[]>([])

  function add(type: NotificationType, title: string, message?: string, duration = 5000): void {
    const id = nextId++
    notifications.value.push({ id, type, title, message, duration })

    if (duration > 0) {
      setTimeout(() => remove(id), duration)
    }
  }

  function remove(id: number): void {
    notifications.value = notifications.value.filter((n) => n.id !== id)
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

  return { notifications, add, remove, success, error, warning, info }
})
