import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { toast } from 'vue-sonner'

import AgentConfigTab from './AgentConfigTab.vue'
import * as agentConfigs from '@/lib/agentConfigs'
import * as providerCatalog from '@/lib/providerCatalog'
import * as harnessApi from '@/services/harness.api'
import type { AgentConfig } from '@/lib/harnessAgents'
import type { ProviderModel } from '@/lib/harnessModels'

vi.mock('vue-sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}))

const catalog: ProviderModel[] = [
  {
    id: 'openrouter/model-big',
    name: 'Big',
    provider: 'openrouter',
    reasoning_efforts: ['low', 'medium', 'high'],
    default_effort: 'medium',
    supports_tools: true,
    context_length: 128_000,
    max_output_tokens: 8_192,
  },
]

const configs: AgentConfig[] = [
  {
    agent: 'build',
    mode: 'primary',
    description: 'Build',
    model: 'openrouter/model-big',
    effort: 'high',
    inherit_model: false,
    effort_strategy: 'fixed',
  },
  {
    agent: 'plan',
    mode: 'primary',
    description: 'Plan',
    model: 'openrouter/model-big',
    effort: '',
    inherit_model: false,
    effort_strategy: 'fixed',
  },
  {
    agent: 'general',
    mode: 'subagent',
    description: 'General',
    model: '',
    effort: '',
    inherit_model: true,
    effort_strategy: 'inherit',
  },
  {
    agent: 'explore',
    mode: 'subagent',
    description: 'Explore',
    model: '',
    effort: '',
    inherit_model: true,
    effort_strategy: 'lowest',
  },
  {
    agent: 'computeruse',
    mode: 'subagent',
    description: 'CU',
    model: 'openrouter/model-big',
    effort: '',
    inherit_model: false,
    effort_strategy: 'fixed',
  },
]

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

function mountTab() {
  setActivePinia(createPinia())
  return mount(AgentConfigTab, {
    global: {
      stubs: {
        ...stubs,
        AgentSConfigPanel: { template: '<div data-testid="agent-s-config-panel" />' },
      },
    },
  })
}

describe('AgentConfigTab', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.clearAllMocks()
    agentConfigs.resetAgentConfigsCache()
    providerCatalog.resetProviderCatalogCache()
    vi.spyOn(agentConfigs, 'loadAgentConfigsCached').mockResolvedValue(configs)
    vi.spyOn(providerCatalog, 'loadProviderModelsCached').mockResolvedValue(catalog)
    vi.spyOn(harnessApi, 'saveAgentConfigs').mockImplementation(async (payload) =>
      payload.map(
        (c, i) =>
          ({
            ...configs[i]!,
            ...c,
            mode: configs[i]!.mode,
            description: configs[i]!.description,
          }) as AgentConfig,
      ),
    )
  })

  it('renders primary rows and inherit strategy selects', async () => {
    const wrapper = mountTab()
    await flushPromises()
    expect(wrapper.find('[data-testid="agent-row-build"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="agent-row-plan"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="agent-row-general"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="agent-strategy-general"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="agent-configs-status"]').text()).toBe('All changes saved')
  })

  it('saves normalized inherit payload with success toast', async () => {
    const wrapper = mountTab()
    await flushPromises()
    // Make dirty: switch computeruse to inherit.
    await wrapper.find('[data-testid="agent-mode-inherit-computeruse"]').trigger('change')
    await flushPromises()
    expect(wrapper.find('[data-testid="agent-configs-status"]').text()).toBe('Unsaved changes')
    await wrapper.find('[data-testid="save-agent-configs"]').trigger('click')
    await flushPromises()
    const save = vi.mocked(harnessApi.saveAgentConfigs)
    expect(save).toHaveBeenCalled()
    const payload = save.mock.calls[0]![0]
    const general = payload.find((c) => c.agent === 'general')!
    expect(general.model).toBe('')
    expect(general.effort).toBe('')
    expect(general.inherit_model).toBe(true)
    const cu = payload.find((c) => c.agent === 'computeruse')!
    expect(cu.model).toBe('')
    expect(cu.inherit_model).toBe(true)
    expect(toast.success).toHaveBeenCalledWith('Agent models saved', { duration: 5000 })
  })

  it('surfaces save failures as error toast', async () => {
    vi.mocked(harnessApi.saveAgentConfigs).mockRejectedValueOnce(new Error('boom'))
    const wrapper = mountTab()
    await flushPromises()
    await wrapper.find('[data-testid="agent-mode-inherit-computeruse"]').trigger('change')
    await wrapper.find('[data-testid="save-agent-configs"]').trigger('click')
    await flushPromises()
    expect(toast.error).toHaveBeenCalledWith('Failed to save agent models', {
      description: 'boom',
      duration: 8000,
    })
  })

  it('renders the Agent-S harness panel below the agent saves', async () => {
    const wrapper = mountTab()
    await flushPromises()
    expect(wrapper.find('[data-testid="agent-s-config-panel"]').exists()).toBe(true)
  })
})
