import { describe, expect, it } from 'vitest'
import { PROVIDER_META, providerMeta } from './providerMeta'

describe('providerMeta', () => {
  it('lists native providers plus a settings-only Claude Agent connection row', () => {
    expect(PROVIDER_META.map((meta) => meta.id)).toEqual([
      'openrouter',
      'chatgpt',
      'amazon-bedrock',
      'openai-compatible',
      'claude-agent',
    ])
    const claude = providerMeta('claude-agent')
    expect(claude?.name).toBe('Claude Agent')
    expect(claude?.description).toContain('Anthropic account')
    const compat = providerMeta('openai-compatible')
    expect(compat?.name).toBe('OpenAI Compatible')
    expect(compat?.description).toContain('UI-TARS')
    expect(compat?.disconnectConfirm).toContain('Disconnect this endpoint?')
    expect(providerMeta(null)).toBeUndefined()
  })
})
