import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import WorkspacesView from './WorkspacesView.vue'
import WorkspaceCard from '@/components/workspaces/WorkspaceCard.vue'
import { getRunnerMetricsLatest } from '@/services/runners.api'
import { WorkspaceStatus, type Runner, type RunnerSystemMetrics, type Workspace } from '@/types'

const workspaceStore = {
  workspaces: [] as Workspace[],
  loading: false,
  error: null,
  getWorkspaceTransitionLabel: () => null,
}
const runnerStore = {
  runners: [{ id: 'runner-1' }, { id: 'runner-2' }] as Runner[],
  fetchRunners: vi.fn(),
}
const push = vi.fn()

vi.mock('@/stores/workspaces', () => ({ useWorkspaceStore: () => workspaceStore }))
vi.mock('@/stores/runners', () => ({ useRunnerStore: () => runnerStore }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push }) }))
vi.mock('@/services/runners.api', () => ({ getRunnerMetricsLatest: vi.fn() }))

function makeWorkspace(id: string, overrides: Partial<Workspace> = {}): Workspace {
  return {
    id,
    runner_id: 'runner-1',
    name: id,
    status: WorkspaceStatus.RUNNING,
    active_operation: null,
    qemu_vcpus: 2,
    qemu_memory_mb: 4096,
    qemu_disk_size_gb: 128,
    desktop_width: 1920,
    desktop_height: 1080,
    created_by_id: 1,
    last_activity_at: '2026-10-16T12:00:00Z',
    auto_stop_timeout_minutes: null,
    auto_stop_at: null,
    delete_requested_at: null,
    delete_started_at: null,
    delete_confirmed_at: null,
    delete_last_error: '',
    delete_attempt_count: 0,
    created_at: '2026-10-16T12:00:00Z',
    updated_at: '2026-10-16T12:00:00Z',
    has_active_session: false,
    runner_online: true,
    credential_ids: [],
    plugin_ids: [],
    credentials_present: false,
    ...overrides,
  }
}

const GIB = 1024 ** 3

function metrics(cpuPercent = 85): RunnerSystemMetrics {
  return {
    runner_id: 'runner-1',
    timestamp: '2026-10-16T12:00:00Z',
    cpu_usage_percent: 10,
    ram_used_bytes: GIB,
    ram_total_bytes: 8 * GIB,
    disk_used_bytes: 900 * GIB,
    disk_total_bytes: 1024 * GIB,
    vm_metrics: {
      running: {
        cpu_usage_percent: cpuPercent,
        ram_used_bytes: GIB,
        ram_total_bytes: 4 * GIB,
        // Host overlay allocation near capacity must not count as a full disk.
        disk_used_bytes: 127 * GIB,
        disk_total_bytes: 128 * GIB,
      },
    },
  }
}

function diskUsage(usedGib: number, freeGib: number, observedAt = new Date().toISOString()) {
  return {
    used_bytes: usedGib * GIB,
    free_bytes: freeGib * GIB,
    total_bytes: (usedGib + freeGib) * GIB,
    observed_at: observedAt,
  }
}

const latest = vi.mocked(getRunnerMetricsLatest)
const mounted: ReturnType<typeof mount>[] = []
function mountView() {
  const wrapper = mount(WorkspacesView, {
    global: { stubs: { CreateWorkspaceDialog: true, WorkspaceActions: true } },
  })
  mounted.push(wrapper)
  return wrapper
}

describe('WorkspacesView workspace storage', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
    workspaceStore.workspaces = [
      makeWorkspace('running', { disk_usage: diskUsage(4, 12) }),
      makeWorkspace('stopped', {
        status: WorkspaceStatus.STOPPED,
        disk_usage: diskUsage(19, 1, new Date(Date.now() - 3 * 3600_000).toISOString()),
      }),
      makeWorkspace('unknown', { runner_id: 'runner-2', disk_usage: null }),
    ]
    latest.mockReset().mockImplementation(async (runnerId) => {
      if (runnerId === 'runner-2') throw new Error('No metrics available')
      return metrics()
    })
    push.mockReset()
  })

  afterEach(() => {
    mounted.splice(0).forEach((wrapper) => wrapper.unmount())
    vi.useRealTimers()
  })

  it('shows free space inside each workspace and hides unmeasured storage', async () => {
    const wrapper = mountView()
    await flushPromises()
    const cards = wrapper.findAllComponents(WorkspaceCard)
    const running = cards[0]!.get('[data-testid="workspace-card-disk"]')
    expect(running.text()).toBe('12.0 GiB free')
    expect(running.attributes('title')).toBe('4.0 GiB of 16.0 GiB used (25%)')
    expect(running.get('[role="progressbar"]').attributes('aria-valuenow')).toBe('25')
    expect(cards[2]!.find('[data-testid="workspace-card-disk"]').exists()).toBe(false)
  })

  it("highlights an almost full disk and dates a stopped workspace's last measurement", async () => {
    const wrapper = mountView()
    await flushPromises()
    const stopped = wrapper
      .findAllComponents(WorkspaceCard)[1]!
      .get('[data-testid="workspace-card-disk"]')
    expect(stopped.get('span').classes()).toContain('text-amber-600')
    expect(stopped.attributes('title')).toBe('19.0 GiB of 20.0 GiB used (95%) · measured 3h ago')
  })

  it('flags high CPU or memory but not a full host overlay as high usage', async () => {
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.findAllComponents(WorkspaceCard)[0]!.text()).toContain('High usage')

    latest.mockResolvedValue(metrics(5))
    await vi.advanceTimersByTimeAsync(10_000)
    for (const card of wrapper.findAllComponents(WorkspaceCard)) {
      expect(card.text()).not.toContain('High usage')
    }
    expect(latest).toHaveBeenCalledWith('runner-1')
    expect(latest).toHaveBeenCalledWith('runner-2')
  })

  it('still opens the selected workspace', async () => {
    const wrapper = mountView()
    await flushPromises()
    await wrapper.findAllComponents(WorkspaceCard)[1]!.get('[data-slot="card"]').trigger('click')
    expect(push).toHaveBeenCalledWith({ name: 'workspace-detail', params: { id: 'stopped' } })
  })
})
