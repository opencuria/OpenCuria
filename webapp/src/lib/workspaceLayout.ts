export const MIN_WORKSPACE_CONTENT_WIDTH = 400
export const MIN_NAVIGATION_WIDTH = 240
export const MIN_LEFT_CHAT_WIDTH = 280
export const DEFAULT_NAVIGATION_WIDTH = 256
export const COLLAPSED_NAVIGATION_WIDTH = 48
export const CHAT_SIDE_STORAGE_KEY = 'opencuria-sidebar-chat-side'
export const NAVIGATION_WIDTH_STORAGE_KEY = 'opencuria-navigation-width'
export const LEFT_CHAT_WIDTH_STORAGE_KEY = 'opencuria-left-chat-width'

export type ChatSide = 'left' | 'right'

export function loadChatSide(): ChatSide {
  try {
    return localStorage.getItem(CHAT_SIDE_STORAGE_KEY) === 'left' ? 'left' : 'right'
  } catch {
    return 'right'
  }
}

export function loadSidebarWidth(key: string, fallback: number, minimum: number): number {
  try {
    const raw = localStorage.getItem(key)
    const width = raw === null ? fallback : Number(raw)
    return Number.isFinite(width) && width >= minimum ? Math.round(width) : fallback
  } catch {
    return fallback
  }
}

export function saveLayoutPreference(key: string, value: string | number): void {
  try {
    localStorage.setItem(key, String(value))
  } catch {
    // Resizing and docking still work when browser storage is unavailable.
  }
}

/** Reserve space for the content and the opposite panel before resizing. */
export function maxSidebarWidth(viewport: number, opposite: number, minimum: number): number {
  return Math.max(minimum, viewport - opposite - MIN_WORKSPACE_CONTENT_WIDTH)
}
