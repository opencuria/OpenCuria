import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useRunnerStore } from './runners'

const api = vi.hoisted(() => ({ list: vi.fn() }))
vi.mock('@/services/runners.api', () => ({
  listRunners: api.list,
  createRunner: vi.fn(),
  updateRunner: vi.fn(),
}))

beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
})

describe('runner store', () => {
  it('marks the first settled load so background refreshes stay silent', async () => {
    const store = useRunnerStore()
    expect(store.loaded).toBe(false)
    api.list.mockResolvedValueOnce([])
    await store.fetchRunners()
    expect(store.loaded).toBe(true)
    expect(store.loading).toBe(false)
  })

  it('keeps an error visible during a retry and clears it only on success', async () => {
    const store = useRunnerStore()
    api.list.mockRejectedValueOnce(new Error('backend down'))
    await store.fetchRunners()
    expect(store.error).toBe('backend down')
    expect(store.loaded).toBe(true)

    let resolve!: (runners: never[]) => void
    api.list.mockReturnValueOnce(new Promise((done) => (resolve = done)))
    const retry = store.fetchRunners()
    expect(store.error).toBe('backend down')
    resolve([])
    await retry
    expect(store.error).toBeNull()
  })
})
