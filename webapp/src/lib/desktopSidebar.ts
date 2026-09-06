export const DEFAULT_SIDEBAR_WIDTH = 320
export const MIN_SIDEBAR_WIDTH = 260
export const MAX_SIDEBAR_WIDTH = 640
export const SIDEBAR_WIDTH_STORAGE_KEY = 'opencuria-desktop-sidebar-width'

/**
 * Clamp a desktop-sidebar width to the allowed range and 60% of the viewport.
 */
export function clampSidebarWidth(width: number, viewportWidth: number): number {
  if (!Number.isFinite(width)) return DEFAULT_SIDEBAR_WIDTH
  const viewportCap = Math.floor(Math.max(viewportWidth, 0) * 0.6)
  const maxWidth = Math.max(
    MIN_SIDEBAR_WIDTH,
    Math.min(MAX_SIDEBAR_WIDTH, viewportCap || MAX_SIDEBAR_WIDTH),
  )
  return Math.min(maxWidth, Math.max(MIN_SIDEBAR_WIDTH, Math.round(width)))
}

/**
 * Read the persisted sidebar width, falling back to the default.
 */
export function loadSidebarWidth(): number {
  try {
    const raw = localStorage.getItem(SIDEBAR_WIDTH_STORAGE_KEY)
    if (raw == null) return DEFAULT_SIDEBAR_WIDTH
    const parsed = Number(raw)
    if (!Number.isFinite(parsed)) return DEFAULT_SIDEBAR_WIDTH
    return clampSidebarWidth(parsed, window.innerWidth)
  } catch {
    return DEFAULT_SIDEBAR_WIDTH
  }
}

/**
 * Persist a clamped sidebar width for later desktop sessions.
 */
export function saveSidebarWidth(width: number): void {
  try {
    const clamped = clampSidebarWidth(width, window.innerWidth)
    localStorage.setItem(SIDEBAR_WIDTH_STORAGE_KEY, String(clamped))
  } catch {
    // Ignore quota / private-mode failures.
  }
}
