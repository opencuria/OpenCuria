import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { toast } from 'vue-sonner'

import AgentConfigTab from './AgentConfigTab.vue'
import * as agentConfigs from '@/lib/agentConfigs'
import * as providerCatalog from '@/lib/providerCatalog'
import * as harnessApi from '@/services/harness.api'
import type { AgentConfig } from '@/lib/harnessAgents'
import type { ProviderModel } from '@/lib/harnessModels'

enableAutoUnmount(afterEach)
afterEach(() => vi.useRealTimers())
async function autosave() {
  await vi.advanceTimersByTimeAsync(500)
  await flushPromises()
}

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
  DropdownMenu: { template: '<div><slot /></div>' },
  DropdownMenuTrigger: { template: '<div><slot /></div>' },
  DropdownMenuContent: { template: '<div><slot /></div>' },
  DropdownMenuItem: { template: '<button type="button"><slot /></button>' },
  DropdownMenuSub: { template: '<div><slot /></div>' },
  DropdownMenuSubTrigger: { template: '<div><slot /></div>' },
  DropdownMenuSubContent: { template: '<div><slot /></div>' },
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
    vi.useFakeTimers()
    vi.restoreAllMocks()
    vi.clearAllMocks()
    agentConfigs.resetAgentConfigsCache()
    providerCatalog.resetProviderCatalogCache()
    vi.spyOn(agentConfigs, 'loadAgentConfigsCached').mockResolvedValue(configs)
    vi.spyOn(providerCatalog, 'loadProviderModelsCached').mockResolvedValue(catalog)
    vi.spyOn(harnessApi, 'getSubagentConfig').mockResolvedValue({ max_depth: 2 })
    vi.spyOn(harnessApi, 'saveSubagentConfig').mockImplementation(async (payload) => payload)
    vi.spyOn(harnessApi, 'saveAgentConfigs').mockImplementation(async (payload) =>
      configs.map(
        (config) =>
          ({
            ...config,
            ...payload.find((item) => item.agent === config.agent),
          }) as AgentConfig,
      ),
    )
  })

  it('uses a single shared model and effort menu per agent', async () => {
    const wrapper = mountTab()
    await flushPromises()
    expect(wrapper.find('[data-testid="agent-row-build"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="agent-row-plan"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="agent-row-general"]').exists()).toBe(true)
    expect(wrapper.findAll('input[type="radio"]')).toHaveLength(0)
    expect(wrapper.findAll('select')).toHaveLength(0)
    for (const agent of ['general', 'explore', 'computeruse']) {
      const row = wrapper.get(`[data-testid="agent-row-${agent}"]`)
      expect(row.findAll('[data-testid="composer-model-trigger"]')).toHaveLength(1)
      expect(row.get('[data-testid="composer-model-default"]').text()).toBe('Inherit')
    }
    expect(
      wrapper.get('[data-testid="agent-row-explore"] [data-testid="composer-effort-row"]').text(),
    ).toContain('Lowest')
    expect(wrapper.find('[data-testid="agent-configs-status"]').text()).toBe('All changes saved')
  })

  it('autosaves normalized inherit payload without success toast spam', async () => {
    const wrapper = mountTab()
    await flushPromises()
    // Make dirty: switch computeruse to inherit.
    await wrapper
      .get('[data-testid="agent-row-computeruse"] [data-testid="composer-model-default"]')
      .trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="agent-configs-status"]').text()).toBe('Waiting to save…')
    await autosave()
    await flushPromises()
    const save = vi.mocked(harnessApi.saveAgentConfigs)
    expect(save).toHaveBeenCalled()
    expect(harnessApi.saveSubagentConfig).not.toHaveBeenCalled()
    const payload = save.mock.calls[0]![0]
    expect(payload).toHaveLength(1)
    const cu = payload.find((c) => c.agent === 'computeruse')!
    expect(cu.model).toBe('')
    expect(cu.inherit_model).toBe(true)
    expect(toast.success).not.toHaveBeenCalled()
  })

  it('surfaces save failures as error toast', async () => {
    vi.mocked(harnessApi.saveAgentConfigs).mockRejectedValueOnce(new Error('boom'))
    const wrapper = mountTab()
    await flushPromises()
    await wrapper
      .get('[data-testid="agent-row-computeruse"] [data-testid="composer-model-default"]')
      .trigger('click')
    await autosave()
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
  it('loads default depth and explains the org-wide limit', async () => {
    const wrapper = mountTab()
    await flushPromises()
    expect(
      (wrapper.get('[data-testid="subagent-max-depth"]').element as HTMLInputElement).value,
    ).toBe('2')
    expect(wrapper.text()).toContain('Main agent is depth 0.')
    expect(wrapper.get('#subagent-depth-scope').text()).toBe('Default: 2 · New runs')
    expect(wrapper.find('[data-testid="save-agent-configs"]').exists()).toBe(false)
  })

  it('integrates depth as the first row of the shared subagent group', async () => {
    const wrapper = mountTab()
    await flushPromises()
    const group = wrapper.get('[data-testid="subagent-settings-group"]')
    expect(
      Array.from(group.element.children).map((row) => row.getAttribute('data-testid')),
    ).toEqual([
      'subagent-depth-row',
      'agent-row-general',
      'agent-row-explore',
      'agent-row-computeruse',
    ])
    const row = group.get('[data-testid="subagent-depth-row"]')
    expect(row.get('label').attributes('for')).toBe('subagent-max-depth')
    expect(row.get('input').attributes('aria-describedby')).toBe(
      'subagent-depth-hint subagent-depth-scope',
    )
    await row.get('input').setValue('0')
    expect(row.get('input').attributes('aria-describedby')).toBe(
      'subagent-depth-hint subagent-depth-scope subagent-depth-error',
    )
  })

  it('preserves inherited effort strategies across model selection and saves fixed defaults', async () => {
    const wrapper = mountTab()
    await flushPromises()
    const row = wrapper.get('[data-testid="agent-row-explore"]')
    await row.get('[data-testid="composer-effort-highest"]').trigger('click')
    await row.get('[data-testid="composer-model-openrouter/model-big"]').trigger('click')
    await autosave()
    await flushPromises()
    expect(vi.mocked(harnessApi.saveAgentConfigs).mock.calls[0]![0]).toContainEqual({
      agent: 'explore',
      model: 'openrouter/model-big',
      effort: 'medium',
      inherit_model: false,
      effort_strategy: 'fixed',
    })
    // Saved fixed configs no longer persist a relative strategy. Choose a new
    // inherited strategy and verify reselection does not reset it.
    await row.get('[data-testid="composer-model-default"]').trigger('click')
    await row.get('[data-testid="composer-effort-lowest"]').trigger('click')
    await row.get('[data-testid="composer-model-default"]').trigger('click')
    expect(row.get('[data-testid="composer-effort-row"]').text()).toContain('Lowest')
    await autosave()
    await flushPromises()
    expect(vi.mocked(harnessApi.saveAgentConfigs).mock.lastCall![0]).toContainEqual({
      agent: 'explore',
      model: '',
      effort: '',
      inherit_model: true,
      effort_strategy: 'lowest',
    })
  })

  it('restores inherited strategy when toggling models before saving', async () => {
    const wrapper = mountTab()
    await flushPromises()
    const row = wrapper.get('[data-testid="agent-row-explore"]')
    await row.get('[data-testid="composer-effort-highest"]').trigger('click')
    await row.get('[data-testid="composer-model-openrouter/model-big"]').trigger('click')
    await row.get('[data-testid="composer-model-default"]').trigger('click')
    expect(row.get('[data-testid="composer-effort-row"]').text()).toContain('Highest')
  })

  it('loads a stored override', async () => {
    vi.mocked(harnessApi.getSubagentConfig).mockResolvedValue({ max_depth: 4 })
    const wrapper = mountTab()
    await flushPromises()
    expect(
      (wrapper.get('[data-testid="subagent-max-depth"]').element as HTMLInputElement).value,
    ).toBe('4')
  })

  it('saves depth alone without primary models and reloads persisted depth', async () => {
    vi.mocked(agentConfigs.loadAgentConfigsCached).mockResolvedValue([])
    let persisted = { max_depth: 2 }
    vi.mocked(harnessApi.getSubagentConfig).mockImplementation(async () => persisted)
    vi.mocked(harnessApi.saveSubagentConfig).mockImplementation(async (payload) => {
      persisted = payload
      return persisted
    })
    const wrapper = mountTab()
    await flushPromises()
    await wrapper.get('[data-testid="subagent-max-depth"]').setValue('3')
    expect(wrapper.get('[data-testid="subagent-depth-status"]').text()).toBe('Waiting to save…')
    await autosave()
    await flushPromises()
    expect(harnessApi.saveSubagentConfig).toHaveBeenCalledWith({ max_depth: 3 })
    expect(harnessApi.saveAgentConfigs).not.toHaveBeenCalled()
    expect(wrapper.get('[data-testid="subagent-depth-status"]').text()).toBe('All changes saved')
    wrapper.unmount()
    const reloaded = mountTab()
    await flushPromises()
    expect(
      (reloaded.get('[data-testid="subagent-max-depth"]').element as HTMLInputElement).value,
    ).toBe('3')
  })

  it.each(['0', '-1', '1.5', '', '2147483648'])(
    'rejects invalid depth %j inline and remains dirty',
    async (value) => {
      const wrapper = mountTab()
      await flushPromises()
      await wrapper.get('[data-testid="subagent-max-depth"]').setValue(value)
      expect(wrapper.get('[data-testid="subagent-depth-error"]').text()).toContain(
        'Enter an integer',
      )
      expect(wrapper.get('[data-testid="subagent-depth-status"]').text()).toBe('Check your input')
      expect(wrapper.find('[data-testid="save-agent-configs"]').exists()).toBe(false)
      await autosave()
      expect(harnessApi.saveSubagentConfig).not.toHaveBeenCalled()
      expect(harnessApi.saveAgentConfigs).not.toHaveBeenCalled()
    },
  )

  it.each([1, 2147483647])('accepts depth bound %s', async (value) => {
    const wrapper = mountTab()
    await flushPromises()
    await wrapper.get('[data-testid="subagent-max-depth"]').setValue(String(value))
    await autosave()
    await flushPromises()
    expect(harnessApi.saveSubagentConfig).toHaveBeenCalledWith({ max_depth: value })
  })

  it('retains depth input and dirty state on save failure', async () => {
    vi.mocked(harnessApi.saveSubagentConfig).mockRejectedValueOnce(new Error('depth failed'))
    const wrapper = mountTab()
    await flushPromises()
    await wrapper.get('[data-testid="subagent-max-depth"]').setValue('5')
    await autosave()
    await flushPromises()
    expect(
      (wrapper.get('[data-testid="subagent-max-depth"]').element as HTMLInputElement).value,
    ).toBe('5')
    expect(wrapper.get('[data-testid="subagent-depth-status"]').text()).toContain('depth failed')
    expect(toast.error).toHaveBeenCalledWith('Failed to save agent settings', {
      description: 'depth failed',
      duration: 8000,
    })
  })

  it.each(['models', 'depth'])(
    'reflects partial success when %s save fails and retries only failure',
    async (failed) => {
      if (failed === 'models')
        vi.mocked(harnessApi.saveAgentConfigs).mockRejectedValueOnce(new Error('failed'))
      else vi.mocked(harnessApi.saveSubagentConfig).mockRejectedValueOnce(new Error('failed'))
      const wrapper = mountTab()
      await flushPromises()
      await wrapper.get('[data-testid="subagent-max-depth"]').setValue('3')
      await wrapper
        .get('[data-testid="agent-row-computeruse"] [data-testid="composer-model-default"]')
        .trigger('click')
      await autosave()
      await flushPromises()
      const status = wrapper.get(
        `[data-testid="${failed === 'models' ? 'agent-configs-status' : 'subagent-depth-status'}"]`,
      )
      expect(status.text()).toContain('failed')
      expect(toast.error).toHaveBeenCalled()
      await status.get('button').trigger('click')
      await flushPromises()
      expect(harnessApi.saveAgentConfigs).toHaveBeenCalledTimes(failed === 'models' ? 2 : 1)
      expect(harnessApi.saveSubagentConfig).toHaveBeenCalledTimes(failed === 'depth' ? 2 : 1)
      expect(wrapper.get('[data-testid="agent-configs-status"]').text()).toBe('All changes saved')
    },
  )

  it('saves a subagent model even when primary models are not configured', async () => {
    vi.mocked(agentConfigs.loadAgentConfigsCached).mockResolvedValue(
      configs.map((c) => ({ ...c, model: '' })),
    )
    const wrapper = mountTab()
    await flushPromises()
    await wrapper.get('[data-testid="subagent-max-depth"]').setValue('3')
    await wrapper
      .get(
        '[data-testid="agent-row-computeruse"] [data-testid="composer-model-openrouter/model-big"]',
      )
      .trigger('click')
    expect(wrapper.find('[data-testid="save-agent-configs"]').exists()).toBe(false)
    await autosave()
    expect(harnessApi.saveAgentConfigs).toHaveBeenCalledWith([
      expect.objectContaining({ agent: 'computeruse', model: 'openrouter/model-big' }),
    ])
    expect(harnessApi.saveSubagentConfig).toHaveBeenCalledWith({ max_depth: 3 })
  })

  it('saves the first primary model without waiting for the other primary model', async () => {
    vi.mocked(agentConfigs.loadAgentConfigsCached).mockResolvedValue([])
    const wrapper = mountTab()
    await flushPromises()
    await wrapper
      .get('[data-testid="agent-row-build"] [data-testid="composer-model-openrouter/model-big"]')
      .trigger('click')
    await autosave()
    expect(harnessApi.saveAgentConfigs).toHaveBeenCalledWith([
      expect.objectContaining({
        agent: 'build',
        model: 'openrouter/model-big',
        inherit_model: false,
      }),
    ])
  })
})
