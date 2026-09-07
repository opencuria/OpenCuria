import { describe, expect, it } from 'vitest'
import { resolveSettingsTab } from './settingsTabs'

describe('resolveSettingsTab', () => {
  it('maps current sheet tabs onto themselves', () => {
    expect(resolveSettingsTab('general')).toBe('general')
    expect(resolveSettingsTab('provider')).toBe('provider')
    expect(resolveSettingsTab('skills')).toBe('skills')
    expect(resolveSettingsTab('credentials')).toBe('credentials')
    expect(resolveSettingsTab('api-keys')).toBe('api-keys')
    expect(resolveSettingsTab('images')).toBe('images')
    expect(resolveSettingsTab('runners')).toBe('runners')
    expect(resolveSettingsTab('credential-services')).toBe('credential-services')
    expect(resolveSettingsTab('image-definitions')).toBe('image-definitions')
  })

  it('maps legacy org-settings tabs', () => {
    expect(resolveSettingsTab('workspace-policies')).toBe('general')
    expect(resolveSettingsTab('organization')).toBe('credential-services')
    expect(resolveSettingsTab('members')).toBe('credential-services')
    expect(resolveSettingsTab('captured-images')).toBe('images')
    expect(resolveSettingsTab('apikeys')).toBe('api-keys')
  })

  it('falls back to general for unknown values', () => {
    expect(resolveSettingsTab('nope')).toBe('general')
    expect(resolveSettingsTab(undefined)).toBe('general')
    expect(resolveSettingsTab(null)).toBe('general')
  })
})
