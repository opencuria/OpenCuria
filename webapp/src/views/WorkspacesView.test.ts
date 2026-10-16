import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import WorkspacesView from './WorkspacesView.vue'
import WorkspaceCard from '@/components/workspaces/WorkspaceCard.vue'
import { getRunnerMetricsLatest } from '@/services/runners.api'
import { RuntimeType, WorkspaceStatus, type Runner, type RunnerSystemMetrics, type Workspace } from '@/types'

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
    runtime_type: RuntimeType.QEMU,
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

function metrics(usedBytes = 2.5 * 1024 ** 3): RunnerSystemMetrics {
  return {
    runner_id: 'runner-1',
    timestamp: '2026-10-16T12:00:00Z',
    cpu_usage_percent: 10,
    ram_used_bytes: 1024 ** 3,
    ram_total_bytes: 8 * 1024 ** 3,
    disk_used_bytes: 900 * 1024 ** 3,
    disk_total_bytes: 1024 ** 4,
    vm_metrics: {
      running: {
        cpu_usage_percent: 85,
        ram_used_bytes: 1024 ** 3,
        ram_total_bytes: 4 * 1024 ** 3,
        disk_used_bytes: usedBytes,
        disk_total_bytes: 128 * 1024 ** 3,
      },
      stopped: {
        cpu_usage_percent: 0,
        ram_used_bytes: 0,
        ram_total_bytes: 0,
        disk_used_bytes: 1.25 * 1024 ** 3,
        disk_total_bytes: 0,
      },
    },
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

describe('WorkspacesView overlay storage', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
    workspaceStore.workspaces = [
      makeWorkspace('running'),
      makeWorkspace('stopped', { status: WorkspaceStatus.STOPPED }),
      makeWorkspace('unknown', { runner_id: 'runner-2' }),
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

  it('renders each overlay allocation, not virtual capacity or host disk usage', async () => {
    const wrapper = mountView()
    await flushPromises()
    const cards = wrapper.findAllComponents(WorkspaceCard)
    expect(cards[0]!.get('[title="Overlay storage used on disk"]').text()).toBe('2.50 GB')
    expect(cards[1]!.get('[title="Overlay storage used on disk"]').text()).toBe('1.25 GB')
    expect(cards[2]!.get('[title="Overlay storage used on disk"]').text()).toBe('—')
    expect(cards[0]!.text()).toContain('High usage')
    expect(cards[1]!.text()).not.toContain('High usage')
    expect(latest).toHaveBeenCalledWith('runner-1')
    expect(latest).toHaveBeenCalledWith('runner-2')
  })

  it('keeps measured zero distinct from unavailable storage', async () => {
    latest.mockResolvedValue(metrics(0))
    const wrapper = mountView()
    await flushPromises()
    const cards = wrapper.findAllComponents(WorkspaceCard)
    expect(cards[0]!.get('[title="Overlay storage used on disk"]').text()).toBe('0 B')
    expect(cards[2]!.get('[title="Overlay storage used on disk"]').text()).toBe('—')
  })

  it('updates the displayed storage on the next metrics poll', async () => {
    const wrapper = mountView()
    await flushPromises()
    latest.mockResolvedValue(metrics(3 * 1024 ** 3))
    await vi.advanceTimersByTimeAsync(10_000)
    expect(wrapper.findAllComponents(WorkspaceCard)[0]!.text()).toContain('3.00 GB')
  })

  it('shows unknown rather than fabricated usage when metrics fail or are forbidden', async () => {
    const wrapper = mountView()
    await flushPromises()
    latest.mockRejectedValue(new Error('Forbidden'))
    await vi.advanceTimersByTimeAsync(10_000)
    for (const card of wrapper.findAllComponents(WorkspaceCard)) {
      expect(card.get('[title="Overlay storage used on disk"]').text()).toBe('—')
      expect(card.text()).not.toContain('High usage')
    }
  })

  it('still opens the selected workspace', async () => {
    const wrapper = mountView()
    await flushPromises()
    await wrapper.findAllComponents(WorkspaceCard)[1]!.get('[data-slot="card"]').trigger('click')
    expect(push).toHaveBeenCalledWith({ name: 'workspace-detail', params: { id: 'stopped' } })
  })
})
