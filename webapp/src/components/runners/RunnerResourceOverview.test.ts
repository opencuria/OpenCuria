import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import type { Runner, RunnerSystemMetrics } from '@/types'
import { RunnerStatus } from '@/types'
import type { StorageRuntime } from '@/types/runnerStorage'
import { getRunnerMetricsHistory, getRunnerMetricsLatest } from '@/services/runners.api'
import RunnerResourceOverview from './RunnerResourceOverview.vue'

vi.mock('@/services/runners.api', () => ({
  getRunnerMetricsLatest: vi.fn(),
  getRunnerMetricsHistory: vi.fn(),
}))
const timestamp = '2026-10-03T00:00:00Z'
const runner = {
  id: 'a',
  status: RunnerStatus.ONLINE,
  available_runtimes: ['qemu'],
  qemu_max_active_vcpus: 8,
  qemu_max_active_memory_mb: null,
  qemu_max_active_disk_size_gb: 50,
  qemu_default_vcpus: 2,
  qemu_min_vcpus: 1,
  qemu_max_vcpus: 4,
  qemu_default_memory_mb: 4096,
  qemu_min_memory_mb: 1024,
  qemu_max_memory_mb: 8192,
  qemu_default_disk_size_gb: 50,
  qemu_min_disk_size_gb: 20,
  qemu_max_disk_size_gb: 100,
} as Runner
const sample: RunnerSystemMetrics = {
  runner_id: 'a',
  timestamp,
  cpu_usage_percent: 70,
  ram_used_bytes: 900,
  ram_total_bytes: 1000,
  disk_used_bytes: 200,
  disk_total_bytes: 1000,
}
const runtime = (name: string): StorageRuntime => ({
  runtime_type: name,
  fresh: true,
  snapshot_id: 1,
  collected_at: timestamp,
  received_at: timestamp,
  filesystems: [{ path: '/data', capacity_bytes: 1000, used_bytes: 300, available_bytes: 600 }],
  resources: [],
  diagnostics: null,
})
const latest = vi.mocked(getRunnerMetricsLatest)
const history = vi.mocked(getRunnerMetricsHistory)
const mounted: ReturnType<typeof mount>[] = []
function overview(r = runner, runtimes: StorageRuntime[] = []) {
  const wrapper = mount(RunnerResourceOverview, { props: { runner: r, runtimes } })
  mounted.push(wrapper)
  return wrapper
}

describe('RunnerResourceOverview', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(timestamp))
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
    latest.mockReset().mockResolvedValue(sample)
    history
      .mockReset()
      .mockResolvedValue([{ ...sample, timestamp: '2026-10-02T23:00:00Z' }, sample])
  })
  afterEach(() => {
    mounted.splice(0).forEach((wrapper) => wrapper.unmount())
    vi.useRealTimers()
  })

  it('shows sampled values, semantic thresholds, free bytes and policy as caps', async () => {
    const wrapper = overview()
    await flushPromises()
    expect(wrapper.text()).toContain('70.0%')
    expect(wrapper.text()).toContain('100 B free')
    expect(wrapper.text()).toContain('800 B host disk free')
    expect(wrapper.find('.bg-warning').exists()).toBe(true)
    expect(wrapper.find('.bg-destructive').exists()).toBe(true)
    expect(wrapper.find('.bg-success').exists()).toBe(true)
    expect(wrapper.text()).toContain('High utilization')
    expect(wrapper.text()).toContain('Unlimited')
    expect(wrapper.find('svg[viewBox="0 0 100 100"]').exists()).toBe(true)
    await wrapper
      .findAll('button')
      .find((button) => button.text().includes('QEMU caps'))!
      .trigger('click')
    expect(wrapper.text()).toContain('Default 2 vCPU · Min 1 · Max 4')
    expect(wrapper.text()).toContain('not actual usage or host capacity')
  })

  it('renders real zero separately from unknown denominator and nonfinite usage', async () => {
    latest.mockResolvedValueOnce({
      ...sample,
      cpu_usage_percent: 0,
      ram_total_bytes: 0,
      disk_used_bytes: NaN,
    })
    const wrapper = overview()
    await flushPromises()
    expect(wrapper.text()).toContain('0.0%')
    expect(wrapper.text()).toContain('Unknown')
    expect(wrapper.html()).not.toContain('NaN')
    expect(wrapper.html()).not.toContain('Infinity')
    expect(wrapper.text()).not.toContain('High utilization')
  })

  it('fetches last-known samples offline and mutes their utilization', async () => {
    const wrapper = overview({ ...runner, status: RunnerStatus.OFFLINE })
    await flushPromises()
    expect(latest).toHaveBeenCalledWith('a')
    expect(wrapper.text()).toContain('70.0%')
    expect(wrapper.text()).toContain('Offline')
    expect(wrapper.text()).toContain('Outdated')
    expect(wrapper.find('.bg-success').exists()).toBe(false)
    expect(wrapper.find('.bg-warning').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('High utilization')
  })

  it('ages samples even when polls keep returning the same timestamp', async () => {
    const wrapper = overview()
    await flushPromises()
    expect(wrapper.text()).not.toContain('Outdated')
    await vi.advanceTimersByTimeAsync(180_000)
    expect(wrapper.text()).toContain('3m ago')
    expect(wrapper.text()).toContain('Outdated')
    expect(wrapper.find('.bg-success').exists()).toBe(false)
  })

  it('keeps each equal-sized filesystem and its own timestamp independent of host sample', async () => {
    const wrapper = overview(runner, [
      runtime('docker'),
      { ...runtime('qemu'), collected_at: '2026-10-02T22:00:00Z', fresh: false },
    ])
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((button) => button.text().includes('Filesystems'))!
      .trigger('click')
    expect(wrapper.text()).toContain('Filesystems (2)')
    expect(wrapper.text()).toContain('docker · /data')
    expect(wrapper.text()).toContain('qemu · /data')
    expect(wrapper.text()).toContain('600 B available')
    expect(wrapper.text()).toContain('Filesystem sample · 0s ago · Recent')
    expect(wrapper.text()).toContain('Filesystem sample · 2h ago · Outdated')
    expect(wrapper.text()).toContain('Host sample · 0s ago')
  })

  it('trusts backend filesystem freshness even beyond the host stale threshold', async () => {
    const wrapper = overview(runner, [
      { ...runtime('docker'), collected_at: '2026-10-02T23:56:00Z' },
    ])
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((button) => button.text().includes('Filesystems'))!
      .trigger('click')
    expect(wrapper.text()).toContain('Filesystem sample · 4m ago · Recent')
  })

  it('emits timestamped usable samples and null when failed, stale or offline', async () => {
    const wrapper = overview()
    expect(wrapper.emitted('sample')?.slice(-1)[0]).toEqual([null])
    await flushPromises()
    expect(wrapper.emitted('sample')?.slice(-1)[0]).toEqual([sample])
    latest.mockRejectedValueOnce(new Error('network'))
    await vi.advanceTimersByTimeAsync(60_000)
    expect(wrapper.emitted('sample')?.slice(-1)[0]).toEqual([null])
    await vi.advanceTimersByTimeAsync(60_000)
    expect(wrapper.emitted('sample')?.slice(-1)[0]).toEqual([sample])
    await vi.advanceTimersByTimeAsync(60_000)
    expect(wrapper.emitted('sample')?.slice(-1)[0]).toEqual([null])
    await wrapper.setProps({ runner: { ...runner, status: RunnerStatus.OFFLINE } })
    expect(wrapper.emitted('sample')?.slice(-1)[0]).toEqual([null])
  })

  it('uses unknown image labels instead of invalid meters for missing or negative usage', async () => {
    latest.mockResolvedValueOnce({ ...sample, cpu_usage_percent: -1, ram_total_bytes: 0 })
    const wrapper = overview()
    await flushPromises()
    expect(wrapper.find('[role="img"][aria-label="CPU utilization unknown"]').exists()).toBe(true)
    expect(
      wrapper
        .find('[role="img"][aria-label="RAM utilization unknown"]')
        .attributes('aria-valuenow'),
    ).toBeUndefined()
    expect(wrapper.find('[role="meter"]').attributes('aria-valuenow')).toBe('20')
    expect(wrapper.text()).not.toContain('not actual usage or host capacity')
  })

  it('announces missing data and offers an accessible retry', async () => {
    latest.mockRejectedValueOnce(new Error('network'))
    const wrapper = overview()
    await flushPromises()
    expect(wrapper.find('[aria-live="polite"]').text()).toContain(
      'Host sample could not be refreshed',
    )
    const retry = wrapper.findAll('button').find((button) => button.text() === 'Retry')!
    await retry.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('70.0%')
    expect(latest).toHaveBeenCalledTimes(2)
  })

  it('does not hide latest values when CPU history fails', async () => {
    history.mockRejectedValueOnce(new Error('network'))
    const wrapper = overview()
    await flushPromises()
    expect(wrapper.text()).toContain('70.0%')
    expect(wrapper.text()).toContain('CPU history could not be refreshed')
    expect(wrapper.text()).not.toContain('Outdated')
  })
})
