export const DEFAULT_PANEL_WIDTH = 400
export const MIN_PANEL_WIDTH = 320
/** Largest share of the viewport the side panel may occupy. */
export const MAX_PANEL_VIEWPORT_FRACTION = 3 / 5
export const PANEL_WIDTH_STORAGE_KEY = 'opencuria-side-panel-width'
export const PANEL_STATE_STORAGE_KEY = 'opencuria-side-panel-state'

export const SIDE_PANEL_TABS = ['git', 'desktop', 'terminal', 'files'] as const
export type SidePanelTab = (typeof SIDE_PANEL_TABS)[number]

export const DEFAULT_PANEL_TAB: SidePanelTab = 'terminal'

/**
 * Clamp a side-panel width between the minimum and 3/5 of the viewport.
 */
export function clampPanelWidth(width: number, viewportWidth: number): number {
  if (!Number.isFinite(width)) return DEFAULT_PANEL_WIDTH
  const viewportCap = Math.floor(Math.max(viewportWidth, 0) * MAX_PANEL_VIEWPORT_FRACTION)
  const maxWidth = Math.max(MIN_PANEL_WIDTH, viewportCap)
  return Math.min(maxWidth, Math.max(MIN_PANEL_WIDTH, Math.round(width)))
}

/**
 * Read the persisted panel width, falling back to the default.
 */
export function loadPanelWidth(): number {
  try {
    const raw = localStorage.getItem(PANEL_WIDTH_STORAGE_KEY)
    if (raw == null) return DEFAULT_PANEL_WIDTH
    const parsed = Number(raw)
    if (!Number.isFinite(parsed)) return DEFAULT_PANEL_WIDTH
    return clampPanelWidth(parsed, window.innerWidth)
  } catch {
    return DEFAULT_PANEL_WIDTH
  }
}

/**
 * Persist a clamped panel width for later sessions.
 */
export function savePanelWidth(width: number): void {
  try {
    const clamped = clampPanelWidth(width, window.innerWidth)
    localStorage.setItem(PANEL_WIDTH_STORAGE_KEY, String(clamped))
  } catch {
    // Ignore quota / private-mode failures.
  }
}

export interface SidePanelState {
  isOpen: boolean
  activeTab: SidePanelTab
}

/**
 * Read the persisted panel visibility and active tab.
 */
export function loadPanelState(): SidePanelState {
  const fallback: SidePanelState = { isOpen: false, activeTab: DEFAULT_PANEL_TAB }
  try {
    const raw = localStorage.getItem(PANEL_STATE_STORAGE_KEY)
    if (raw == null) return fallback
    const parsed = JSON.parse(raw) as Partial<SidePanelState>
    const activeTab = SIDE_PANEL_TABS.includes(parsed.activeTab as SidePanelTab)
      ? (parsed.activeTab as SidePanelTab)
      : DEFAULT_PANEL_TAB
    return { isOpen: parsed.isOpen === true, activeTab }
  } catch {
    return fallback
  }
}

/**
 * Persist the panel visibility and active tab for later sessions.
 */
export function savePanelState(state: SidePanelState): void {
  try {
    localStorage.setItem(PANEL_STATE_STORAGE_KEY, JSON.stringify(state))
  } catch {
    // Ignore quota / private-mode failures.
  }
}
