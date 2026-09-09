/**
 * Shared helpers for the global Settings sheet.
 *
 * Kept in a lightweight module (no component imports) so that
 * OrgSettingsView can map legacy `?tab=` deep-links onto sheet tabs
 * without pulling the whole sheet into its bundle.
 */

/** Window event that opens the Settings sheet. Detail: `{ tab?: string }`. */
export const OPEN_SETTINGS_EVENT = 'opencuria:open-settings'

/** Query param on `/` that opens the sheet via deep-link (`/?settings=<tab>`). */
export const SETTINGS_QUERY_PARAM = 'settings'

export type SettingsTabId =
  | 'general'
  | 'provider'
  | 'agents'
  | 'skills'
  | 'credentials'
  | 'api-keys'
  | 'images'
  | 'runners'
  | 'credential-services'
  | 'image-definitions'

/**
 * Maps old OrgSettings query tabs (`workspace-policies`, `provider`,
 * `image-definitions`, `credential-services`) and free-form input
 * (event detail, `?settings=`) onto sheet tabs. Unknown → `general`.
 */
export function resolveSettingsTab(tab: unknown): SettingsTabId {
  switch (tab) {
    case 'general':
    case 'workspace-policies':
      return 'general'
    case 'provider':
      return 'provider'
    case 'agents':
      return 'agents'
    case 'skills':
      return 'skills'
    case 'credentials':
      return 'credentials'
    case 'api-keys':
    case 'apikeys':
    case 'api_keys':
      return 'api-keys'
    case 'images':
    case 'captured-images':
      return 'images'
    case 'runners':
      return 'runners'
    case 'credential-services':
    case 'organization':
    case 'organisation':
    case 'members':
      return 'credential-services'
    case 'image-definitions':
      return 'image-definitions'
    default:
      return 'general'
  }
}
