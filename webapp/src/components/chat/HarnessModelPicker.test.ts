import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import HarnessModelPicker from './HarnessModelPicker.vue'
import type { ProviderModel } from '@/lib/harnessModels'

const models: ProviderModel[] = [
  {
    id: 'openrouter/think',
    name: 'Think',
    provider: 'openrouter',
    reasoning_efforts: ['low', 'high'],
    default_effort: 'high',
    supports_tools: true,
    context_length: 128_000,
    max_output_tokens: 8_192,
  },
  {
    id: 'chatgpt/plain',
    name: 'Plain',
    provider: 'chatgpt',
    reasoning_efforts: [],
    default_effort: '',
    supports_tools: true,
    context_length: 0,
    max_output_tokens: 0,
  },
  {
    id: 'openrouter/extra-1',
    name: 'Extra 1',
    provider: 'openrouter',
    reasoning_efforts: ['low'],
    default_effort: 'low',
    supports_tools: true,
    context_length: 0,
    max_output_tokens: 0,
  },
  {
    id: 'openrouter/extra-2',
    name: 'Extra 2',
    provider: 'openrouter',
    reasoning_efforts: ['low'],
    default_effort: 'low',
    supports_tools: true,
    context_length: 0,
    max_output_tokens: 0,
  },
  {
    id: 'openrouter/extra-3',
    name: 'Extra 3',
    provider: 'openrouter',
    reasoning_efforts: ['low'],
    default_effort: 'low',
    supports_tools: true,
    context_length: 0,
    max_output_tokens: 0,
  },
  {
    id: 'openrouter/extra-4',
    name: 'Extra 4',
    provider: 'openrouter',
    reasoning_efforts: ['low'],
    default_effort: 'low',
    supports_tools: true,
    context_length: 0,
    max_output_tokens: 0,
  },
  {
    id: 'openrouter/extra-5',
    name: 'Extra 5',
    provider: 'openrouter',
    reasoning_efforts: ['low'],
    default_effort: 'low',
    supports_tools: true,
    context_length: 0,
    max_output_tokens: 0,
  },
  {
    id: 'openrouter/extra-6',
    name: 'Extra 6',
    provider: 'openrouter',
    reasoning_efforts: ['low'],
    default_effort: 'low',
    supports_tools: true,
    context_length: 0,
    max_output_tokens: 0,
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

describe('HarnessModelPicker', () => {
  it('lists catalog models without an Auto entry', () => {
    const wrapper = mount(HarnessModelPicker, {
      props: { model: 'openrouter/think', effort: 'high', models },
      global: { stubs },
    })
    expect(wrapper.text()).not.toContain('Auto')
    expect(wrapper.find('[data-testid="composer-model-auto"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('Think')
    expect(wrapper.text()).toContain('Plain')
    expect(wrapper.text()).not.toContain('Fast')
    expect(wrapper.find('[data-testid="composer-effort-row"]').exists()).toBe(true)
  })

  it('shows provider labels next to each model row', () => {
    const wrapper = mount(HarnessModelPicker, {
      props: {
        model: 'openrouter/think',
        effort: 'high',
        models,
        recentModels: [models[0]!, models[1]!],
      },
      global: { stubs },
    })
    const think = wrapper.find('[data-testid="composer-model-openrouter/think"]')
    const plain = wrapper.find('[data-testid="composer-model-chatgpt/plain"]')
    expect(think.text()).toContain('OpenRouter')
    expect(plain.text()).toContain('ChatGPT')
  })

  it('shows the model without duplicating the separately selected effort', () => {
    const wrapper = mount(HarnessModelPicker, {
      props: { model: 'openrouter/think', effort: 'high', models },
      global: { stubs },
    })
    const trigger = wrapper.find('[data-testid="composer-model-trigger"]')
    expect(trigger.text()).toContain('Think')
    expect(trigger.text()).not.toContain('High')
    expect(wrapper.find('[data-testid="composer-effort-row"]').text()).toContain('High')
  })

  it('hides the effort submenu when the model has no reasoning', () => {
    const wrapper = mount(HarnessModelPicker, {
      props: { model: 'chatgpt/plain', effort: '', models },
      global: { stubs },
    })
    expect(wrapper.find('[data-testid="composer-effort-row"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="composer-model-trigger"]').text()).toContain('Plain')
    expect(wrapper.find('[data-testid="composer-model-trigger"]').text()).not.toContain('High')
  })

  it('shows a placeholder trigger when no model is selected', () => {
    const wrapper = mount(HarnessModelPicker, {
      props: { model: '', effort: '', models },
      global: { stubs },
    })
    expect(wrapper.find('[data-testid="composer-model-trigger"]').text()).toContain(
      'Select model\u2026',
    )
  })

  it('filters the catalog by model name and provider label', async () => {
    const wrapper = mount(HarnessModelPicker, {
      props: { model: '', effort: '', models },
      global: { stubs },
    })
    await wrapper.find('[data-testid="composer-model-search"]').setValue('chatgpt')
    expect(wrapper.text()).toContain('Plain')
    expect(wrapper.text()).not.toContain('Think')
  })
})

describe('HarnessModelPicker recent/all', () => {
  it('defaults to recent (max 6) with an All Models button', () => {
    const wrapper = mount(HarnessModelPicker, {
      props: { model: 'openrouter/think', effort: 'high', models },
      global: { stubs },
    })
    // 8 in catalog, only first 6 visible in Recent mode.
    expect(wrapper.text()).toContain('Think')
    expect(wrapper.text()).not.toContain('Extra 6')
    const all = wrapper.find('[data-testid="composer-model-show-all"]')
    expect(all.exists()).toBe(true)
    expect(all.text()).toContain('All Models (8)')
    expect(wrapper.text()).toContain('Recent')
  })

  it('shows only the passed recents first', () => {
    const wrapper = mount(HarnessModelPicker, {
      props: {
        model: '',
        effort: '',
        models,
        recentModels: [models[1]!],
        recentEfforts: [{ id: 'chatgpt/plain', effort: '' }],
      },
      global: { stubs },
    })
    expect(wrapper.text()).toContain('Plain')
    expect(wrapper.text()).not.toContain('Think')
  })

  it('reveals all models after clicking All Models', async () => {
    const wrapper = mount(HarnessModelPicker, {
      props: { model: '', effort: '', models },
      global: { stubs },
    })
    await wrapper.find('[data-testid="composer-model-show-all"]').trigger('click')
    expect(wrapper.text()).toContain('Extra 6')
    expect(wrapper.text()).toContain('All models')
    expect(wrapper.find('[data-testid="composer-model-show-recent"]').exists()).toBe(true)
  })

  it('searches the full catalog even in recent mode', async () => {
    const wrapper = mount(HarnessModelPicker, {
      props: { model: '', effort: '', models },
      global: { stubs },
    })
    await wrapper.find('[data-testid="composer-model-search"]').setValue('extra-6')
    expect(wrapper.text()).toContain('Extra 6')
    expect(wrapper.text()).not.toContain('Think')
    expect(wrapper.find('[data-testid="composer-model-show-all"]').exists()).toBe(false)
  })

  it('restores the last-used effort when selecting a model', async () => {
    const wrapper = mount(HarnessModelPicker, {
      props: {
        model: '',
        effort: 'low',
        models,
        recentModels: [models[0]!],
        recentEfforts: [{ id: 'openrouter/think', effort: 'high' }],
      },
      global: { stubs },
    })
    await wrapper.find('[data-testid="composer-model-openrouter/think"]').trigger('click')
    expect(wrapper.emitted('update:model')).toEqual([['openrouter/think']])
    expect(wrapper.emitted('update:effort')).toEqual([['high']])
  })
})
