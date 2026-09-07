/**
 * Shared helpers for the global Settings-Sheet (Schritt 5).
 *
 * Kept in a lightweight module (no component imports) so that
 * OrgSettingsView can map legacy `?tab=` deep-links onto sheet tabs
 * without pulling the whole sheet (5 Views) into its bundle.
 */

/** Window-Event, das das Settings-Sheet öffnet. Detail: `{ tab?: string }`. */
export const OPEN_SETTINGS_EVENT = 'opencuria:open-settings'

/** Query-Param auf `/`, der das Sheet per Deep-Link öffnet (`/?settings=<tab>`). */
export const SETTINGS_QUERY_PARAM = 'settings'

export type SettingsTabId =
  | 'general'
  | 'provider'
  | 'skills'
  | 'credentials'
  | 'api-keys'
  | 'images'
  | 'runners'
  | 'organization'

/**
 * Mappt alte OrgSettings-Query-Tabs (`workspace-policies`, `provider`,
 * `image-definitions`, `credential-services`) und freie Eingaben
 * (Event-Detail, `?settings=`) auf Sheet-Tabs. Unbekannt → `general`.
 */
export function resolveSettingsTab(tab: unknown): SettingsTabId {
  switch (tab) {
    case 'general':
    case 'workspace-policies':
      return 'general'
    case 'provider':
      return 'provider'
    case 'skills':
      return 'skills'
    case 'credentials':
      return 'credentials'
    case 'api-keys':
    case 'apikeys':
    case 'api_keys':
      return 'api-keys'
    case 'images':
    case 'image-definitions':
    case 'captured-images':
      return 'images'
    case 'runners':
      return 'runners'
    case 'organization':
    case 'organisation':
    case 'credential-services':
    case 'members':
      return 'organization'
    default:
      return 'general'
  }
}
