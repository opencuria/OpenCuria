import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ComputerUseConfigPanel from './ComputerUseConfigPanel.vue'
import * as api from '@/services/harness.api'
vi.mock('@/services/harness.api', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/services/harness.api')>(),
  getComputerUseConfig: vi.fn(), saveComputerUseConfig: vi.fn(),
}))
enableAutoUnmount(afterEach)
afterEach(() => vi.useRealTimers())
const defaults = { max_steps: 15, screenshot_max_dimension: 2400, enable_recording: false }
describe('ComputerUseConfigPanel', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.useFakeTimers()
    vi.clearAllMocks()
    vi.mocked(api.getComputerUseConfig).mockResolvedValue(defaults)
    vi.mocked(api.saveComputerUseConfig).mockImplementation(async data => ({ ...defaults, ...data }))
  })
  it('loads and autosaves only changed execution limits', async () => {
    const wrapper = mount(ComputerUseConfigPanel)
    await flushPromises()
    expect(wrapper.text()).toContain('Computer Use')
    await wrapper.get('#computer-use-max-steps').setValue('20')
    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()
    expect(api.saveComputerUseConfig).toHaveBeenCalledWith({ max_steps: 20 })
  })
  it('rejects invalid dimensions without saving', async () => {
    const wrapper = mount(ComputerUseConfigPanel)
    await flushPromises()
    await wrapper.get('#computer-use-screenshot-size').setValue('10')
    await vi.advanceTimersByTimeAsync(1000)
    expect(wrapper.text()).toContain('320–7680')
    expect(api.saveComputerUseConfig).not.toHaveBeenCalled()
  })
  it('shows load errors', async () => {
    vi.mocked(api.getComputerUseConfig).mockRejectedValue(new Error('Unavailable'))
    const wrapper = mount(ComputerUseConfigPanel)
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toBe('Unavailable')
  })
})
