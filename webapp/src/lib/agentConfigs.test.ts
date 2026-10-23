import { beforeEach, describe, expect, it, vi } from 'vitest'
import { listAgentConfigs } from '@/services/harness.api'
import {
  invalidateAgentConfigs,
  loadAgentConfigsCached,
  resetAgentConfigsCache,
} from './agentConfigs'

vi.mock('@/services/harness.api', () => ({ listAgentConfigs: vi.fn() }))
vi.mock('./providerCatalog', () => ({
  providerCatalogContext: () => localStorage.getItem('kern_active_org_id') ?? '',
}))

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  resetAgentConfigsCache()
  vi.mocked(listAgentConfigs).mockResolvedValue([])
})

describe('agent config cache', () => {
  it('reuses a request within the current organization until invalidated', async () => {
    await Promise.all([loadAgentConfigsCached(), loadAgentConfigsCached()])
    expect(listAgentConfigs).toHaveBeenCalledTimes(1)
    invalidateAgentConfigs()
    await loadAgentConfigsCached()
    expect(listAgentConfigs).toHaveBeenCalledTimes(2)
  })

  it('fetches again when the organization changes', async () => {
    localStorage.setItem('kern_active_org_id', 'first')
    await loadAgentConfigsCached()
    localStorage.setItem('kern_active_org_id', 'second')
    await loadAgentConfigsCached()
    expect(listAgentConfigs).toHaveBeenCalledTimes(2)
  })

  it('does not evict a new organization request when an older request fails', async () => {
    let rejectFirst!: (reason: Error) => void
    vi.mocked(listAgentConfigs).mockImplementationOnce(
      () =>
        new Promise((_, reject) => {
          rejectFirst = reject
        }),
    )
    const old = loadAgentConfigsCached().catch(() => [])
    localStorage.setItem('kern_active_org_id', 'second')
    const current = loadAgentConfigsCached()
    rejectFirst(new Error('offline'))
    await old
    expect(loadAgentConfigsCached()).toBe(current)
  })
})
