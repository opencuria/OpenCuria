/**
 * Unit tests for the recent-models lib (normalize, cap, LRU, catalog filter).
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  MAX_RECENT_MODELS,
  loadRecentModels,
  orderByRecency,
  recentCatalogModels,
  recentEffortFor,
  recordRecentModelUsage,
  resetRecentModelsCache,
  useRecentModels,
} from './recentModels'
import * as harnessApi from '@/services/harness.api'
import type { ProviderModel } from '@opencuria/chat-core/harnessModels'

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return { ...actual, listRecentModels: vi.fn(), saveRecentModel: vi.fn() }
})

const listMock = vi.mocked(harnessApi.listRecentModels)
const saveMock = vi.mocked(harnessApi.saveRecentModel)

function catalogModel(id: string): ProviderModel {
  return {
    id,
    name: id,
    provider: 'openrouter',
    reasoning_efforts: ['low', 'high'],
    default_effort: 'low',
    supports_tools: true,
    context_length: 1000,
    max_output_tokens: 100,
  }
}

describe('recentModels', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    resetRecentModelsCache()
  })

  it('caps entries at MAX_RECENT_MODELS (6)', () => {
    expect(MAX_RECENT_MODELS).toBe(6)
  })

  it('normalizes server rows (trim, dedupe, cap)', async () => {
    listMock.mockResolvedValue([
      { model: '  b ', effort: 'High', last_used_at: '2026-01-01T00:00:00Z' },
      { model: 'a', effort: '', last_used_at: '2026-01-02T00:00:00Z' },
      { model: 'b', effort: 'low', last_used_at: '2026-01-03T00:00:00Z' },
      { model: '', effort: 'low', last_used_at: '2026-01-04T00:00:00Z' },
      { model: 'c', effort: '', last_used_at: '' },
      { model: 'd', effort: '', last_used_at: '' },
      { model: 'e', effort: '', last_used_at: '' },
      { model: 'f', effort: '', last_used_at: '' },
      { model: 'g', effort: '', last_used_at: '' },
    ])
    const entries = await loadRecentModels()
    expect(entries.map((entry) => entry.id)).toEqual(['b', 'a', 'c', 'd', 'e', 'f'])
    expect(recentEffortFor('b')).toBe('high')
    expect(recentEffortFor('missing')).toBe('')
    expect(entries[0]).toMatchObject({ harnessId: 'native', mode: 'build' })
  })

  it('returns [] when the fetch fails', async () => {
    listMock.mockRejectedValue(new Error('down'))
    await expect(loadRecentModels()).resolves.toEqual([])
    expect(useRecentModels().entries.value).toEqual([])
  })

  it('keeps only catalog-known models in recency order', async () => {
    listMock.mockResolvedValue([
      { model: 'gone/model', effort: '', last_used_at: '' },
      { model: 'b', effort: 'high', last_used_at: '' },
      { model: 'a', effort: 'low', last_used_at: '' },
    ])
    await loadRecentModels()
    const models = [catalogModel('a'), catalogModel('b'), catalogModel('c')]
    expect(recentCatalogModels(models).map((item) => item.id)).toEqual(['b', 'a'])
  })

  it('orders ids by recency, unknown ids last in catalog order', async () => {
    listMock.mockResolvedValue([
      { model: 'b', effort: '', last_used_at: '' },
      { model: 'a', effort: '', last_used_at: '' },
    ])
    await loadRecentModels()
    expect(orderByRecency(['c', 'a', 'b', 'd'])).toEqual(['b', 'a', 'c', 'd'])
  })

  it('records usage optimistically (LRU) and persists fire-and-forget', async () => {
    listMock.mockResolvedValue([{ model: 'a', effort: 'low', last_used_at: '' }])
    await loadRecentModels()
    saveMock.mockResolvedValue({ model: 'b', effort: 'high', last_used_at: '' })

    recordRecentModelUsage('b', 'High')
    expect(useRecentModels().entries.value.map((entry) => entry.id)).toEqual(['b', 'a'])
    expect(recentEffortFor('b')).toBe('high')
    await vi.waitFor(() => {
      expect(saveMock).toHaveBeenCalledWith('b', 'high', undefined, {
        harness_id: 'native', mode: 'build',
      })
    })

    // Save failures never throw (send must not break).
    saveMock.mockRejectedValueOnce(new Error('down'))
    expect(() => recordRecentModelUsage('c', '')).not.toThrow()
  })

  it('ignores blank model ids', () => {
    recordRecentModelUsage('   ')
    expect(useRecentModels().entries.value).toEqual([])
    expect(saveMock).not.toHaveBeenCalled()
  })

  it('remembers the full last-used agent selection', async () => {
    listMock.mockResolvedValue([])
    await loadRecentModels()
    recordRecentModelUsage('model', 'High', 'codex', 'plan')
    expect(useRecentModels().entries.value[0]).toEqual({
      id: 'model', effort: 'high', harnessId: 'codex', mode: 'plan',
    })
    await vi.waitFor(() => {
      expect(saveMock).toHaveBeenCalledWith('model', 'high', undefined, {
        harness_id: 'codex', mode: 'plan',
      })
    })
  })

  it('does not reuse preferences or pending responses across organizations', async () => {
    let resolveFirst!: (rows: harnessApi.RecentModelUsage[]) => void
    listMock.mockImplementationOnce(() => new Promise(resolve => { resolveFirst = resolve }))
    const first = loadRecentModels()
    localStorage.setItem('kern_active_org_id', 'other-org')
    try {
      listMock.mockResolvedValueOnce([
        { model: 'other', effort: 'low', harness_id: 'native', mode: 'build', last_used_at: '' },
      ])
      await loadRecentModels()
      resolveFirst([
        { model: 'private', effort: 'high', harness_id: 'codex', mode: 'plan', last_used_at: '' },
      ])
      await first
      expect(useRecentModels().entries.value.map(entry => entry.id)).toEqual(['other'])
    } finally {
      localStorage.removeItem('kern_active_org_id')
    }
  })

  it('keeps a successful send newer than an in-flight preferences load', async () => {
    let resolve!: (rows: harnessApi.RecentModelUsage[]) => void
    listMock.mockImplementationOnce(() => new Promise(done => { resolve = done }))
    const loading = loadRecentModels()
    recordRecentModelUsage('new', 'high', 'codex', 'plan')
    resolve([{ model: 'old', effort: 'low', last_used_at: '' }])
    await loading
    expect(useRecentModels().entries.value[0]).toMatchObject({ id: 'new', harnessId: 'codex', mode: 'plan' })
  })
})
