import { afterEach, describe, expect, it, vi } from 'vitest'

import * as harnessApi from '@/services/harness.api'
import type { ProviderModel } from './harnessModels'
import {
  invalidateProviderCatalog,
  loadProviderModelsCached,
  resetProviderCatalogCache,
} from './providerCatalog'

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    listProviderModels: vi.fn(),
  }
})

const listProviderModelsMock = vi.mocked(harnessApi.listProviderModels)

const catalog: ProviderModel[] = [
  {
    id: 'openrouter/think',
    name: 'Think',
    provider: 'openrouter',
    reasoning_efforts: ['high'],
    default_effort: 'high',
    supports_tools: true,
    context_length: 1,
    max_output_tokens: 1,
  },
]

describe('loadProviderModelsCached', () => {
  afterEach(() => {
    resetProviderCatalogCache()
    vi.clearAllMocks()
  })

  it('coalesces concurrent callers onto one request', async () => {
    listProviderModelsMock.mockResolvedValue(catalog)
    const [first, second] = await Promise.all([
      loadProviderModelsCached(),
      loadProviderModelsCached(),
    ])
    expect(first).toEqual(catalog)
    expect(second).toEqual(catalog)
    expect(listProviderModelsMock).toHaveBeenCalledTimes(1)
  })

  it('retries after a failed fetch', async () => {
    listProviderModelsMock.mockRejectedValueOnce(new Error('down'))
    listProviderModelsMock.mockResolvedValueOnce(catalog)
    await expect(loadProviderModelsCached()).rejects.toThrow('down')
    await expect(loadProviderModelsCached()).resolves.toEqual(catalog)
    expect(listProviderModelsMock).toHaveBeenCalledTimes(2)
  })

  it('invalidates the cached promise so the next load refetches', async () => {
    listProviderModelsMock.mockResolvedValue(catalog)
    await loadProviderModelsCached()
    invalidateProviderCatalog()
    await loadProviderModelsCached()
    expect(listProviderModelsMock).toHaveBeenCalledTimes(2)
  })
})
