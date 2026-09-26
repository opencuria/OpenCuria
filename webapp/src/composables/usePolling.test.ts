import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, nextTick, ref, type Ref } from 'vue'
import { mount, flushPromises } from '@vue/test-utils'
import { usePolling } from './usePolling'

function createPoller(fetchFn: () => Promise<void>, interval: Ref<number> = ref(1000)) {
  let controls!: ReturnType<typeof usePolling>
  const wrapper = mount(defineComponent({
    setup() {
      controls = usePolling(fetchFn, interval)
      return () => null
    },
  }))
  return { controls, wrapper }
}

function setVisibility(state: DocumentVisibilityState): void {
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: state })
  document.dispatchEvent(new Event('visibilitychange'))
}

describe('usePolling', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('runs immediately once and schedules the next run after completion', async () => {
    const fetchFn = vi.fn().mockResolvedValue(undefined)
    const { controls, wrapper } = createPoller(fetchFn)

    controls.start()
    expect(fetchFn).toHaveBeenCalledTimes(1)
    await flushPromises()
    await vi.advanceTimersByTimeAsync(999)
    expect(fetchFn).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(fetchFn).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('does not overlap slow requests or queue interval callbacks', async () => {
    let resolve!: () => void
    const fetchFn = vi.fn(() => new Promise<void>((done) => { resolve = done }))
    const { controls, wrapper } = createPoller(fetchFn)
    controls.start()

    await vi.advanceTimersByTimeAsync(5000)
    expect(fetchFn).toHaveBeenCalledTimes(1)
    resolve()
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1000)
    expect(fetchFn).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('pauses while hidden and refreshes exactly once when visible again', async () => {
    const fetchFn = vi.fn().mockResolvedValue(undefined)
    const { controls, wrapper } = createPoller(fetchFn)
    controls.start()
    await flushPromises()
    setVisibility('hidden')
    await vi.advanceTimersByTimeAsync(5000)
    expect(fetchFn).toHaveBeenCalledTimes(1)

    setVisibility('visible')
    expect(fetchFn).toHaveBeenCalledTimes(2)
    setVisibility('visible')
    expect(fetchFn).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('runs once on return if an earlier fetch is still in flight', async () => {
    let resolve!: () => void
    const fetchFn = vi.fn(() => new Promise<void>((done) => { resolve = done }))
    const { controls, wrapper } = createPoller(fetchFn)
    controls.start()
    setVisibility('hidden')
    setVisibility('visible')
    setVisibility('visible')
    expect(fetchFn).toHaveBeenCalledTimes(1)

    resolve()
    await flushPromises()
    expect(fetchFn).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('restarts a reactive interval without triggering an extra fetch', async () => {
    const interval = ref(1000)
    const fetchFn = vi.fn().mockResolvedValue(undefined)
    const { controls, wrapper } = createPoller(fetchFn, interval)
    controls.start()
    await flushPromises()
    interval.value = 2000
    await nextTick()

    await vi.advanceTimersByTimeAsync(1000)
    expect(fetchFn).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1000)
    expect(fetchFn).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('stops timers and visibility listeners when stopped or unmounted', async () => {
    const fetchFn = vi.fn().mockResolvedValue(undefined)
    const { controls, wrapper } = createPoller(fetchFn)
    controls.start()
    await flushPromises()
    controls.stop()
    await vi.advanceTimersByTimeAsync(5000)
    expect(fetchFn).toHaveBeenCalledTimes(1)
    expect(controls.isPolling.value).toBe(false)

    controls.start()
    await flushPromises()
    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(5000)
    expect(fetchFn).toHaveBeenCalledTimes(2)
  })

  it('continues polling after a rejected fetch', async () => {
    const fetchFn = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue(undefined)
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const { controls, wrapper } = createPoller(fetchFn)
    controls.start()
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1000)
    expect(fetchFn).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })
})
