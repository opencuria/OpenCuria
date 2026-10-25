/** Shared helpers for the global Settings sheet. */

/** Window event that opens Settings with context IDs (never a workspace form payload). */
export const OPEN_SETTINGS_EVENT = 'opencuria:open-settings'
export const CLOSE_SETTINGS_EVENT = 'opencuria:close-settings'

/** Query param on `/` that opens the sheet via deep-link (`/?settings=<tab>`). */
export const SETTINGS_QUERY_PARAM = 'settings'

const SETTINGS_TABS = [
  'general',
  'provider',
  'agents',
  'skills',
  'credentials',
  'plugins',
  'api-keys',
  'images',
  'runners',
  'credential-services',
  'image-definitions',
] as const

export type SettingsTabId = (typeof SETTINGS_TABS)[number]

/** Resolve event or deep-link input to a current tab, defaulting to General. */
export function resolveSettingsTab(tab: unknown): SettingsTabId {
  return SETTINGS_TABS.find((id) => id === tab) ?? 'general'
}
