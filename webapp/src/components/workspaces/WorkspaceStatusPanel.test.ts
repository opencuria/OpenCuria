import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import WorkspaceStatusPanel from './WorkspaceStatusPanel.vue'
import { getRunnerMetricsLatest } from '@/services/runners.api'
import { WorkspaceStatus, type RunnerSystemMetrics, type Workspace } from '@/types'

vi.mock('@/services/runners.api', () => ({ getRunnerMetricsLatest: vi.fn() }))
vi.mock('@/stores/runners', () => ({ useRunnerStore: () => ({ runnerById: () => undefined }) }))

const GIB = 1024 ** 3
const latest = vi.mocked(getRunnerMetricsLatest)

function makeWorkspace(overrides: Partial<Workspace> = {}): Workspace {
  return {
    id: 'ws-1',
    runner_id: 'runner-1',
    status: WorkspaceStatus.RUNNING,
    qemu_vcpus: 2,
    qemu_memory_mb: 4096,
    qemu_disk_size_gb: 20,
    disk_usage: {
      used_bytes: 5 * GIB,
      free_bytes: 13 * GIB,
      total_bytes: 19 * GIB,
      observed_at: new Date().toISOString(),
    },
    ...overrides,
  } as Workspace
}

function liveMetrics(timestamp = new Date().toISOString()): RunnerSystemMetrics {
  return {
    runner_id: 'runner-1',
    timestamp,
    cpu_usage_percent: 50,
    ram_used_bytes: 8 * GIB,
    ram_total_bytes: 16 * GIB,
    disk_used_bytes: 0,
    disk_total_bytes: 0,
    vm_metrics: {
      'ws-1': {
        cpu_usage_percent: 12.4,
        ram_used_bytes: 1 * GIB,
        ram_total_bytes: 4 * GIB,
        disk_used_bytes: 3 * GIB,
        disk_total_bytes: 20 * GIB,
      },
    },
  }
}

const mounted: ReturnType<typeof mount>[] = []
function mountPanel(workspace: Workspace, runnerOffline = false) {
  const wrapper = mount(WorkspaceStatusPanel, { props: { workspace, runnerOffline } })
  mounted.push(wrapper)
  return wrapper
}

describe('WorkspaceStatusPanel', () => {
  beforeEach(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
    latest.mockReset().mockResolvedValue(liveMetrics())
  })

  afterEach(() => {
    mounted.splice(0).forEach((wrapper) => wrapper.unmount())
  })

  it('shows storage, live memory and CPU of a running workspace calmly', async () => {
    const wrapper = mountPanel(makeWorkspace())
    await flushPromises()

    const storage = wrapper.get('[data-testid="workspace-status-storage"]')
    expect(storage.text()).toContain('13.0 GiB free')
    expect(storage.text()).toContain('5.0 GiB of 18.0 GiB used')
    expect(storage.text()).toContain('27%')
    expect(wrapper.find('[data-testid="workspace-status-disk-warning"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="workspace-status-memory"]').text()).toContain(
      '1.0 GiB of 4.0 GiB',
    )
    expect(wrapper.get('[data-testid="workspace-status-cpu"]').text()).toContain('12%')
    expect(wrapper.get('[data-testid="workspace-status-allocation"]').text()).toBe(
      '2 vCPU · 4.0 GiB RAM · 20.0 GiB disk',
    )
    expect(wrapper.find('[data-testid="workspace-status-age"]').exists()).toBe(false)
    expect(latest).toHaveBeenCalledWith('runner-1')
  })

  it('offers to increase an almost full disk', async () => {
    const wrapper = mountPanel(
      makeWorkspace({
        disk_usage: {
          used_bytes: 17 * GIB,
          free_bytes: 1 * GIB,
          total_bytes: 19 * GIB,
          observed_at: new Date().toISOString(),
        },
      }),
    )
    await flushPromises()

    const warning = wrapper.get('[data-testid="workspace-status-disk-warning"]')
    expect(warning.text()).toContain('Disk almost full')
    await warning.get('[data-testid="workspace-status-increase-disk"]').trigger('click')
    expect(wrapper.emitted('increase-disk')).toHaveLength(1)
  })

  it('keeps the last storage reading of a stopped workspace without live metrics', async () => {
    const observedAt = new Date(Date.now() - 2 * 3600_000).toISOString()
    const wrapper = mountPanel(
      makeWorkspace({
        status: WorkspaceStatus.STOPPED,
        disk_usage: {
          used_bytes: GIB,
          free_bytes: GIB,
          total_bytes: 2 * GIB,
          observed_at: observedAt,
        },
      }),
    )
    await flushPromises()

    expect(wrapper.get('[data-testid="workspace-status-storage-free"]').text()).toBe('1.0 GiB free')
    expect(wrapper.get('[data-testid="workspace-status-age"]').text()).toBe(
      'Storage measured 2h ago',
    )
    expect(wrapper.find('[data-testid="workspace-status-memory"]').exists()).toBe(false)
    expect(latest).not.toHaveBeenCalled()
  })

  it('ignores stale or failed live metrics and explains unmeasured storage', async () => {
    latest.mockResolvedValue(liveMetrics(new Date(Date.now() - 10 * 60_000).toISOString()))
    const stale = mountPanel(makeWorkspace({ disk_usage: null }))
    await flushPromises()
    expect(stale.find('[data-testid="workspace-status-cpu"]').exists()).toBe(false)
    expect(stale.get('[data-testid="workspace-status-storage"]').text()).toContain(
      'Measured while the workspace is running.',
    )

    latest.mockRejectedValue(new Error('Forbidden'))
    const failed = mountPanel(makeWorkspace())
    await flushPromises()
    expect(failed.find('[data-testid="workspace-status-memory"]').exists()).toBe(false)
  })
})
