import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { toast } from 'vue-sonner'

import AgentSConfigPanel from './AgentSConfigPanel.vue'
import * as providerCatalog from '@/lib/providerCatalog'
import * as harnessApi from '@/services/harness.api'
import type { AgentSConfig } from '@/services/harness.api'

vi.mock('vue-sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}))

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return { ...actual, getAgentSConfig: vi.fn(), saveAgentSConfig: vi.fn() }
})

const getAgentSConfigMock = vi.mocked(harnessApi.getAgentSConfig)
const saveAgentSConfigMock = vi.mocked(harnessApi.saveAgentSConfig)

const defaults: AgentSConfig = {
  grounding_model: '',
  grounding_width: 1920,
  grounding_height: 1080,
  model_temperature: null,
  max_steps: 15,
  max_trajectory_length: 8,
  enable_reflection: true,
  enable_code_agent: true,
  screenshot_max_dimension: 2400,
  action_pre_delay: 1.0,
  action_post_delay: 1.0,
  wait_delay: 5.0,
}

const stubs = {
  Popover: { template: '<div><slot /></div>' },
  PopoverTrigger: { template: '<div><slot /></div>' },
  PopoverContent: { template: '<div><slot /></div>' },
  Command: { template: '<div><slot /></div>' },
  CommandInput: { template: '<input />' },
  CommandList: { template: '<div><slot /></div>' },
  CommandEmpty: { template: '<div><slot /></div>' },
  CommandGroup: { props: ['heading'], template: '<div><slot /></div>' },
  CommandItem: { template: '<button type="button" @click="$emit(\'select\')"><slot /></button>' },
}

function mountPanel() {
  setActivePinia(createPinia())
  return mount(AgentSConfigPanel, { global: { stubs } })
}

describe('AgentSConfigPanel', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.clearAllMocks()
    vi.spyOn(providerCatalog, 'loadProviderModelsCached').mockResolvedValue([])
    getAgentSConfigMock.mockResolvedValue({ ...defaults })
    saveAgentSConfigMock.mockImplementation(async (data) => ({ ...defaults, ...data }))
  })

  it('loads defaults and explains the Computer Use main model', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    expect(getAgentSConfigMock).toHaveBeenCalled()
    expect(wrapper.find('[data-testid="agent-s-config-panel"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="agent-s-main-model-hint"]').text()).toContain('Computer Use')
    expect(wrapper.find('[data-testid="agent-s-goto-computeruse"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="agent-s-config-status"]').text()).toBe('All changes saved')
    expect(wrapper.find('[data-testid="save-agent-s-config"]').attributes('disabled')).toBeDefined()
  })

  it('tracks dirty state and saves only changed fields', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.find('[data-testid="agent-s-max-steps"]').setValue('20')
    expect(wrapper.find('[data-testid="agent-s-config-status"]').text()).toBe('Unsaved changes')

    await wrapper.find('[data-testid="save-agent-s-config"]').trigger('click')
    await flushPromises()

    expect(saveAgentSConfigMock).toHaveBeenCalledWith({ max_steps: 20 })
    expect(toast.success).toHaveBeenCalledWith('Agent-S settings saved', { duration: 5000 })
    expect(wrapper.find('[data-testid="agent-s-config-status"]').text()).toBe('All changes saved')
  })

  it('sends partial payload for multiple fields incl. temperature and toggles', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.find('[data-testid="agent-s-temperature"]').setValue('0.7')
    await wrapper.find('[data-testid="agent-s-enable-reflection"]').setValue(false)
    await wrapper.find('[data-testid="save-agent-s-config"]').trigger('click')
    await flushPromises()

    expect(saveAgentSConfigMock).toHaveBeenCalledWith({
      model_temperature: 0.7,
      enable_reflection: false,
    })
  })

  it('sends null temperature when the cleared field was set', async () => {
    getAgentSConfigMock.mockResolvedValue({ ...defaults, model_temperature: 0.5 })
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.find('[data-testid="agent-s-temperature"]').setValue('')
    await wrapper.find('[data-testid="save-agent-s-config"]').trigger('click')
    await flushPromises()

    expect(saveAgentSConfigMock).toHaveBeenCalledWith({ model_temperature: null })
  })

  it('blocks invalid numbers with inline errors instead of saving', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.find('[data-testid="agent-s-max-steps"]').setValue('0')
    await wrapper.find('[data-testid="agent-s-temperature"]').setValue('9')
    await wrapper.find('[data-testid="agent-s-pre-delay"]').setValue('-1')
    await wrapper.find('[data-testid="save-agent-s-config"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="agent-s-error-max-steps"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="agent-s-error-temperature"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="agent-s-error-pre-delay"]').exists()).toBe(true)
    expect(saveAgentSConfigMock).not.toHaveBeenCalled()
  })

  it('surfaces load and save failures', async () => {
    getAgentSConfigMock.mockRejectedValueOnce(new Error('load boom'))
    const failedLoad = mountPanel()
    await flushPromises()
    expect(failedLoad.find('[data-testid="agent-s-config-error"]').text()).toContain('load boom')

    saveAgentSConfigMock.mockRejectedValueOnce(new Error('save boom'))
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.find('[data-testid="agent-s-max-steps"]').setValue('21')
    await wrapper.find('[data-testid="save-agent-s-config"]').trigger('click')
    await flushPromises()

    expect(toast.error).toHaveBeenCalledWith('Failed to save Agent-S settings', {
      description: 'save boom',
      duration: 8000,
    })
  })
})
