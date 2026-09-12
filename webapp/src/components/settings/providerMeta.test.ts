import { describe, expect, it } from 'vitest'
import { PROVIDER_META, connectionDetail, providerMeta } from './providerMeta'

describe('providerMeta', () => {
  it('lists four providers including the OpenAI-compatible endpoint', () => {
    expect(PROVIDER_META.map((meta) => meta.id)).toEqual([
      'openrouter',
      'chatgpt',
      'amazon-bedrock',
      'openai-compatible',
    ])
    const compat = providerMeta('openai-compatible')
    expect(compat?.name).toBe('OpenAI Compatible')
    expect(compat?.description).toContain('UI-TARS')
    expect(compat?.disconnectConfirm).toContain('Disconnect this endpoint?')
    expect(providerMeta(null)).toBeUndefined()
  })

  it('details the compatible connection with base URL and model count', () => {
    expect(
      connectionDetail({
        provider: 'openai-compatible',
        connected: true,
        base_url: 'https://my-host:8000/v1',
        models: ['a', 'b'],
      }),
    ).toBe('https://my-host:8000/v1 · 2 models')
    expect(
      connectionDetail({
        provider: 'openai-compatible',
        connected: true,
        base_url: 'https://my-host:8000/v1',
        models: ['a'],
      }),
    ).toBe('https://my-host:8000/v1 · 1 model')
    expect(
      connectionDetail({
        provider: 'openai-compatible',
        connected: true,
        base_url: 'https://my-host:8000/v1',
        models: [],
      }),
    ).toBe('https://my-host:8000/v1')
    expect(connectionDetail({ provider: 'openai-compatible', connected: true })).toBe('Connected')
    expect(connectionDetail({ provider: 'openai-compatible', connected: false })).toBe('')
  })
})
