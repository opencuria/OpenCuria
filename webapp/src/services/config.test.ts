import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

describe('runtime config loader', () => {
  beforeEach(() => {
    vi.resetModules()
    vi.restoreAllMocks()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('falls back to defaults when config.json request fails', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network failure')))
    const { loadConfig } = await import('./config')

    const config = await loadConfig()

    expect(config.apiBaseUrl).toBe('/api/v1')
    expect(config.wsBaseUrl).toBe('')
  })

  it('uses values from /config.json when available', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          apiBaseUrl: '/custom-api',
          wsBaseUrl: 'ws://localhost:9999',
        }),
      }),
    )

    const { getConfig, loadConfig } = await import('./config')
    const config = await loadConfig()

    expect(config).toEqual({
      apiBaseUrl: '/custom-api',
      wsBaseUrl: 'ws://localhost:9999',
    })
    expect(getConfig()).toEqual(config)
  })
})
