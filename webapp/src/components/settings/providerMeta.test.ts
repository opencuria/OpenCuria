import { describe, expect, it } from 'vitest'
import { PROVIDER_META, providerMeta } from './providerMeta'

describe('providerMeta', () => {
  it('gives every shared provider a web icon', () => {
    expect(PROVIDER_META.map((meta) => meta.id)).toEqual([
      'openrouter',
      'chatgpt',
      'amazon-bedrock',
      'openai-compatible',
      'claude-agent',
    ])
    expect(PROVIDER_META.every((meta) => meta.icon)).toBe(true)
    expect(providerMeta('claude-agent')?.name).toBe('Claude Agent')
    expect(providerMeta(null)).toBeUndefined()
  })
})
