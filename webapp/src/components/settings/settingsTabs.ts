/** Shared helpers for the global Settings sheet. */

/** Window event that opens Settings with context IDs (never a workspace form payload). */
export const OPEN_SETTINGS_EVENT = 'opencuria:open-settings'
export const CLOSE_SETTINGS_EVENT = 'opencuria:close-settings'
/** Window event fired after the sheet closed, so views can reload what Settings may change. */
export const SETTINGS_CLOSED_EVENT = 'opencuria:settings-closed'

/** Query param on `/` that opens the sheet via deep-link (`/?settings=<tab>`). */
export const SETTINGS_QUERY_PARAM = 'settings'

const SETTINGS_TABS = [
  'general',
  'provider',
  'agents',
  'credentials',
  'plugins',
  'api-keys',
  'images',
  'runners',
  'credential-services',
  'image-definitions',
  'windows',
] as const

export type SettingsTabId = (typeof SETTINGS_TABS)[number]

/** Skills live inside plugins; old `skills` links open Plugins. */
const RENAMED_TABS: Record<string, SettingsTabId> = { skills: 'plugins' }

/** Resolve event or deep-link input to a current tab, defaulting to General. */
export function resolveSettingsTab(tab: unknown): SettingsTabId {
  const name = typeof tab === 'string' ? (RENAMED_TABS[tab] ?? tab) : tab
  return SETTINGS_TABS.find((id) => id === name) ?? 'general'
}
