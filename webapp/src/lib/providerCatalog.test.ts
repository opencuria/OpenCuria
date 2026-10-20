import { afterEach, describe, expect, it, vi } from 'vitest'

import * as harnessApi from '@/services/harness.api'
import type { ProviderModel } from './harnessModels'
import {
  invalidateProviderCatalog,
  loadProviderModelsCached,
  loadEngineModelsCached,
  resetProviderCatalogCache,
} from './providerCatalog'

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    listProviderModels: vi.fn(),
    listEngineModels: vi.fn(),
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

  it('keys external catalogs by engine, user and organization and invalidates them on account changes', async () => {
    const external = vi.mocked(harnessApi.listEngineModels).mockResolvedValue(catalog)
    localStorage.setItem('kern_user', JSON.stringify({ id: 10 }))
    localStorage.setItem('kern_active_org_id', 'org-a')
    await Promise.all([loadEngineModelsCached('codex'), loadEngineModelsCached('codex')])
    expect(external).toHaveBeenCalledTimes(1)
    localStorage.setItem('kern_active_org_id', 'org-b')
    await loadEngineModelsCached('codex')
    localStorage.setItem('kern_user', JSON.stringify({ id: 11 }))
    await loadEngineModelsCached('codex')
    window.dispatchEvent(new Event(harnessApi.PROVIDER_ACCOUNTS_CHANGED_EVENT))
    await loadEngineModelsCached('codex')
    expect(external).toHaveBeenCalledTimes(4)
    external.mockRejectedValueOnce(new Error('Unauthorized'))
    invalidateProviderCatalog()
    await expect(loadEngineModelsCached('codex')).rejects.toThrow('Unauthorized')
    await expect(loadEngineModelsCached('codex')).resolves.toEqual(catalog)
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

  it('does not reuse cached catalog data after logout or when the active user changes', async () => {
    localStorage.setItem('kern_user', JSON.stringify({ id: 1 }))
    localStorage.setItem('kern_active_org_id', 'org-a')
    listProviderModelsMock.mockResolvedValue(catalog)
    await loadProviderModelsCached()
    localStorage.setItem('kern_user', JSON.stringify({ id: 2 }))
    await loadProviderModelsCached()
    resetProviderCatalogCache()
    localStorage.removeItem('kern_user')
    await loadProviderModelsCached()
    expect(listProviderModelsMock).toHaveBeenCalledTimes(3)
  })
})
