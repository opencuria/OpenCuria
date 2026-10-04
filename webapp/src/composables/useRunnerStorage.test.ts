import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { defineComponent, ref } from 'vue'
import { useRunnerStorage, storageBytes } from './useRunnerStorage'
import type { RunnerStorage } from '@/types/runnerStorage'
const api = vi.hoisted(() => ({
  getRunnerStorage: vi.fn(),
  listDeletions: vi.fn(),
  refreshRunnerStorage: vi.fn(),
}))
vi.mock('@/services/runnerStorage.api', () => api)
vi.mock('./usePolling', () => ({ usePolling: (fn: () => void) => ({ start: fn }) }))
const snapshot = (id: string, complete = true): RunnerStorage => ({
  runner_id: id,
  runner_online: true,
  latest_snapshot_id: 1,
  latest_complete: complete,
  runtimes: [
    {
      runtime_type: 'qemu',
      fresh: complete,
      received_at: '2026-10-03T12:01:00Z',
      collected_at: '2026-10-03T12:00:59Z',
      snapshot_id: 1,
      filesystems: [],
      resources: [],
      diagnostics: null,
    },
  ],
  generations: [],
  operations: [],
  capture_requests: [],
})
describe('runner storage evidence', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.listDeletions.mockResolvedValue([])
  })
  it('keeps nullable measurements distinct from zero', () => {
    expect(storageBytes(null)).toBe('Unknown')
    expect(storageBytes(undefined)).toBe('Unknown')
    expect(storageBytes(0)).toBe('0 B')
  })
  it('rejects stale replies when selecting another runner and after unmount', async () => {
    let old!: (value: RunnerStorage) => void
    api.getRunnerStorage.mockImplementation((id: string) =>
      id === 'a'
        ? new Promise((resolve) => {
            old = resolve
          })
        : Promise.resolve(snapshot('b')),
    )
    const id = ref('a')
    let storage!: ReturnType<typeof useRunnerStorage>
    const wrapper = mount(
      defineComponent({
        setup() {
          storage = useRunnerStorage(id)
          return () => null
        },
      }),
    )
    id.value = 'b'
    await flushPromises()
    expect(storage.data.value?.runner_id).toBe('b')
    old(snapshot('a'))
    await flushPromises()
    expect(storage.data.value?.runner_id).toBe('b')
    wrapper.unmount()
    await storage.load()
    expect(storage.data.value?.runner_id).toBe('b')
  })
  it('refresh remains queued until new complete fresh evidence and retains data on errors', async () => {
    api.getRunnerStorage.mockResolvedValue(snapshot('a', false))
    api.refreshRunnerStorage.mockResolvedValue({ requested_at: '2026-10-03T12:00:00Z' })
    let storage!: ReturnType<typeof useRunnerStorage>
    const wrapper = mount(
      defineComponent({
        setup() {
          storage = useRunnerStorage(ref('a'))
          return () => null
        },
      }),
    )
    await flushPromises()
    await storage.refresh()
    expect(storage.refreshPending.value).toBe(true)
    api.getRunnerStorage.mockRejectedValue(new Error('Network lost'))
    await storage.load()
    expect(storage.error.value).toBe('Network lost')
    expect(storage.data.value?.runner_id).toBe('a')
    api.getRunnerStorage.mockResolvedValue(snapshot('a'))
    await storage.load()
    expect(storage.refreshPending.value).toBe(false)
    expect(storage.error.value).toBe('')
    wrapper.unmount()
  })
})
