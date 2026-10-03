import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, nextTick, ref } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import type { RunnerSystemMetrics } from '@/types'
import { getRunnerMetricsHistory, getRunnerMetricsLatest } from '@/services/runners.api'
import { resourcePercent, sampleOutdated, useRunnerMetrics } from './useRunnerMetrics'

vi.mock('@/services/runners.api', () => ({
  getRunnerMetricsLatest: vi.fn(),
  getRunnerMetricsHistory: vi.fn(),
}))
const sample: RunnerSystemMetrics = {
  runner_id: 'a',
  timestamp: '2026-10-03T00:00:00Z',
  cpu_usage_percent: 30,
  ram_used_bytes: 10,
  ram_total_bytes: 100,
  disk_used_bytes: 20,
  disk_total_bytes: 100,
}
const latest = vi.mocked(getRunnerMetricsLatest)
const history = vi.mocked(getRunnerMetricsHistory)
function setup() {
  const id = ref('a')
  let state!: ReturnType<typeof useRunnerMetrics>
  const wrapper = mount(
    defineComponent({
      setup() {
        state = useRunnerMetrics(id)
        return () => null
      },
    }),
  )
  return { state, id, wrapper }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

describe('runner metrics', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(sample.timestamp))
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
    latest.mockReset().mockResolvedValue(sample)
    history.mockReset().mockResolvedValue([sample])
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('guards unknown, zero denominator, nonfinite and clamps percentages', () => {
    expect(resourcePercent(0, 100)).toBe(0)
    expect(resourcePercent(70, 100)).toBe(70)
    expect(resourcePercent(200, 100)).toBe(100)
    expect(resourcePercent(-20, 100)).toBeNull()
    for (const result of [
      resourcePercent(null),
      resourcePercent(undefined),
      resourcePercent(NaN),
      resourcePercent(Infinity),
      resourcePercent(0, 0),
      resourcePercent(1, Infinity),
    ])
      expect(result).toBeNull()
    expect(sampleOutdated(sample.timestamp, Date.parse(sample.timestamp) + 180_000)).toBe(true)
    expect(sampleOutdated(null, Date.now())).toBe(true)
  })

  it('loads immediately and preserves cached latest on failure while updating history independently', async () => {
    const { state, wrapper } = setup()
    await flushPromises()
    latest.mockRejectedValueOnce(new Error('network'))
    history.mockResolvedValueOnce([{ ...sample, cpu_usage_percent: 55 }])
    await state.load()
    expect(state.metrics.value).toEqual(sample)
    expect(state.history.value[0]?.cpu_usage_percent).toBe(55)
    expect(state.error.value).toContain('Host sample')
    expect(state.loading.value).toBe(false)
    wrapper.unmount()
  })

  it('keeps latest usable and cached history when history fails', async () => {
    const { state, wrapper } = setup()
    await flushPromises()
    latest.mockResolvedValueOnce({ ...sample, cpu_usage_percent: 80 })
    history.mockRejectedValueOnce(new Error('network'))
    await state.load()
    expect(state.metrics.value?.cpu_usage_percent).toBe(80)
    expect(state.history.value).toEqual([sample])
    expect(state.latestError.value).toBe('')
    expect(state.error.value).toContain('CPU history')
    wrapper.unmount()
  })

  it('publishes latest before slow history settles', async () => {
    const slow = deferred<RunnerSystemMetrics[]>()
    history.mockReturnValueOnce(slow.promise)
    const { state, wrapper } = setup()
    await flushPromises()
    expect(state.metrics.value).toEqual(sample)
    expect(state.loading.value).toBe(true)
    slow.resolve([])
    await flushPromises()
    expect(state.loading.value).toBe(false)
    wrapper.unmount()
  })

  it('ignores out-of-order refresh responses', async () => {
    const slow = deferred<RunnerSystemMetrics>()
    latest.mockReturnValueOnce(slow.promise)
    const { state, wrapper } = setup()
    await state.load()
    slow.resolve({ ...sample, cpu_usage_percent: 99 })
    await flushPromises()
    expect(state.metrics.value?.cpu_usage_percent).toBe(30)
    wrapper.unmount()
  })

  it('clears another runner cache and ignores old responses after id change', async () => {
    const slow = deferred<RunnerSystemMetrics>()
    latest.mockReturnValueOnce(slow.promise)
    const { state, id, wrapper } = setup()
    latest.mockResolvedValueOnce({ ...sample, runner_id: 'b' })
    id.value = 'b'
    await nextTick()
    await flushPromises()
    slow.resolve(sample)
    await flushPromises()
    expect(state.metrics.value?.runner_id).toBe('b')
    expect(latest).toHaveBeenLastCalledWith('b')
    wrapper.unmount()
  })

  it('ignores unmounted responses and cleans up polling, clock and listeners', async () => {
    const slow = deferred<RunnerSystemMetrics>()
    latest.mockReturnValueOnce(slow.promise)
    const { state, wrapper } = setup()
    wrapper.unmount()
    slow.resolve(sample)
    await flushPromises()
    expect(state.metrics.value).toBeNull()
    await vi.advanceTimersByTimeAsync(120_000)
    document.dispatchEvent(new Event('visibilitychange'))
    expect(latest).toHaveBeenCalledTimes(1)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('polls every 60 seconds and advances the freshness clock', async () => {
    const { state, wrapper } = setup()
    await flushPromises()
    await vi.advanceTimersByTimeAsync(60_000)
    expect(latest).toHaveBeenCalledTimes(2)
    expect(state.now.value).toBe(Date.parse(sample.timestamp) + 60_000)
    wrapper.unmount()
    expect(vi.getTimerCount()).toBe(0)
  })
})
