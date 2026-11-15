import { describe, expect, it } from 'vitest'
import { resolveSettingsTab } from './settingsTabs'

describe('resolveSettingsTab', () => {
  it.each([
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
  ])('accepts the current tab %s', (tab) => {
    expect(resolveSettingsTab(tab)).toBe(tab)
  })

  it('falls back to general for unknown values', () => {
    expect(resolveSettingsTab('nope')).toBe('general')
    expect(resolveSettingsTab('notifications')).toBe('general')
    expect(resolveSettingsTab(undefined)).toBe('general')
    expect(resolveSettingsTab(null)).toBe('general')
  })
})
