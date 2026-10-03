import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

import ModelPicker from './ModelPicker.vue'
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

describe('ModelPicker', () => {
  it('lists catalog models without an Auto entry', () => {
    const wrapper = mount(ModelPicker, {
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

  it('offers agent-default reset only when enabled', async () => {
    const regular = mount(ModelPicker, {
      props: { model: 'openrouter/think', effort: 'high', models },
      global: { stubs },
    })
    expect(regular.find('[data-testid="composer-model-default"]').exists()).toBe(false)
    expect(regular.find('[data-testid="composer-effort-default"]').exists()).toBe(false)

    const scheduled = mount(ModelPicker, {
      props: { model: 'openrouter/think', effort: 'high', models, allowDefault: true },
      global: { stubs },
    })
    await scheduled.find('[data-testid="composer-model-default"]').trigger('click')
    expect(scheduled.emitted('update:model')).toEqual([['']])
    expect(scheduled.emitted('update:effort')).toEqual([['']])
    expect(scheduled.find('[data-testid="composer-effort-default"]').exists()).toBe(true)
  })

  it('shows provider labels next to each model row', () => {
    const wrapper = mount(ModelPicker, {
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

  it('does not invent an effort when catalog data arrives for an inherited default model', async () => {
    const wrapper = mount(ModelPicker, {
      props: { model: 'openrouter/think', effort: '', models: [] },
      global: { stubs },
    })
    await wrapper.setProps({ models })
    await nextTick()
    expect(wrapper.emitted('update:effort')).toBeUndefined()
  })

  it('shows the model without duplicating the separately selected effort', () => {
    const wrapper = mount(ModelPicker, {
      props: { model: 'openrouter/think', effort: 'high', models },
      global: { stubs },
    })
    const trigger = wrapper.find('[data-testid="composer-model-trigger"]')
    expect(trigger.text()).toContain('Think')
    expect(trigger.text()).not.toContain('High')
    expect(wrapper.find('[data-testid="composer-effort-row"]').text()).toContain('High')
  })

  it('hides the effort submenu when the model has no reasoning', () => {
    const wrapper = mount(ModelPicker, {
      props: { model: 'chatgpt/plain', effort: '', models },
      global: { stubs },
    })
    expect(wrapper.find('[data-testid="composer-effort-row"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="composer-model-trigger"]').text()).toContain('Plain')
    expect(wrapper.find('[data-testid="composer-model-trigger"]').text()).not.toContain('High')
  })

  it('shows a placeholder trigger when no model is selected', () => {
    const wrapper = mount(ModelPicker, {
      props: { model: '', effort: '', models },
      global: { stubs },
    })
    expect(wrapper.find('[data-testid="composer-model-trigger"]').text()).toContain(
      'Select model\u2026',
    )
  })

  it('filters the catalog by model name and provider label', async () => {
    const wrapper = mount(ModelPicker, {
      props: { model: '', effort: '', models },
      global: { stubs },
    })
    await wrapper.find('[data-testid="composer-model-search"]').setValue('chatgpt')
    expect(wrapper.text()).toContain('Plain')
    expect(wrapper.text()).not.toContain('Think')
  })
})

describe('ModelPicker recent/all', () => {
  it('defaults to recent (max 6) with an All Models button', () => {
    const wrapper = mount(ModelPicker, {
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
    const wrapper = mount(ModelPicker, {
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
    const wrapper = mount(ModelPicker, {
      props: { model: '', effort: '', models },
      global: { stubs },
    })
    await wrapper.find('[data-testid="composer-model-show-all"]').trigger('click')
    expect(wrapper.text()).toContain('Extra 6')
    expect(wrapper.text()).toContain('All models')
    expect(wrapper.find('[data-testid="composer-model-show-recent"]').exists()).toBe(true)
  })

  it('searches the full catalog even in recent mode', async () => {
    const wrapper = mount(ModelPicker, {
      props: { model: '', effort: '', models },
      global: { stubs },
    })
    await wrapper.find('[data-testid="composer-model-search"]').setValue('extra-6')
    expect(wrapper.text()).toContain('Extra 6')
    expect(wrapper.text()).not.toContain('Think')
    expect(wrapper.find('[data-testid="composer-model-show-all"]').exists()).toBe(false)
  })

  it('restores the last-used effort when selecting a model', async () => {
    const wrapper = mount(ModelPicker, {
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

describe('ModelPicker settings', () => {
  it('uses Inherit as the first model option and keeps effort strategies inside the menu', async () => {
    const wrapper = mount(ModelPicker, {
      props: {
        model: '',
        effort: 'lowest',
        models,
        allowDefault: true,
        defaultModelLabel: 'Inherit',
        defaultOptionLabel: 'Inherit',
        variant: 'field',
        inheritedEffortOptions: [
          { value: 'inherit', label: 'Inherit' },
          { value: 'lowest', label: 'Lowest' },
        ],
      },
      global: { stubs },
    })
    expect(wrapper.get('[data-testid="composer-model-trigger"]').text()).toBe('Inherit')
    expect(wrapper.get('[data-testid="composer-model-default"]').text()).toBe('Inherit')
    expect(wrapper.get('[data-testid="composer-effort-row"]').text()).toContain('Lowest')
    expect(wrapper.findAll('select')).toHaveLength(0)
    expect(wrapper.find('[data-testid="composer-effort-default"]').exists()).toBe(false)
    await wrapper.get('[data-testid="composer-effort-inherit"]').trigger('click')
    expect(wrapper.emitted('update:effort')).toEqual([['inherit']])
  })

  it('does not mistake inherited Medium strategy for an explicit fixed effort', async () => {
    const wrapper = mount(ModelPicker, {
      props: {
        model: '',
        effort: 'medium',
        models: [{ ...models[0]!, reasoning_efforts: ['low', 'medium', 'high'] }],
        inheritedEffortOptions: [{ value: 'medium', label: 'Medium' }],
        effortFallback: 'model-default',
      },
      global: { stubs },
    })
    await wrapper.get('[data-testid="composer-model-openrouter/think"]').trigger('click')
    expect(wrapper.emitted('update:effort')).toEqual([['high']])
  })

  it('ignores changes from already-open content after becoming disabled', async () => {
    const wrapper = mount(ModelPicker, {
      props: { model: 'openrouter/think', effort: 'high', models },
      global: { stubs },
    })
    await wrapper.setProps({ disabled: true })
    await wrapper.get('[data-testid="composer-model-chatgpt/plain"]').trigger('click')
    await wrapper.get('[data-testid="composer-effort-low"]').trigger('click')
    expect(wrapper.emitted('update:model')).toBeUndefined()
    expect(wrapper.emitted('update:effort')).toBeUndefined()
  })

  it('keeps the normal composer effort empty but settings may opt into a model default', async () => {
    const regular = mount(ModelPicker, {
      props: { model: '', effort: '', models },
      global: { stubs },
    })
    await regular.get('[data-testid="composer-model-openrouter/think"]').trigger('click')
    expect(regular.emitted('update:effort')).toBeUndefined()
    const settings = mount(ModelPicker, {
      props: { model: '', effort: '', models, effortFallback: 'model-default' },
      global: { stubs },
    })
    await settings.get('[data-testid="composer-model-openrouter/think"]').trigger('click')
    expect(settings.emitted('update:effort')).toEqual([['high']])
  })

  it('allows provider-default effort without making model inheritance available', async () => {
    const wrapper = mount(ModelPicker, {
      props: {
        model: 'openrouter/think',
        effort: 'high',
        models,
        allowEffortDefault: true,
        defaultEffortLabel: 'Provider default',
      },
      global: { stubs },
    })
    expect(wrapper.find('[data-testid="composer-model-default"]').exists()).toBe(false)
    const reset = wrapper.get('[data-testid="composer-effort-default"]')
    expect(reset.text()).toBe('Provider default')
    await reset.trigger('click')
    expect(wrapper.emitted('update:effort')).toEqual([['']])
  })

  it('supports grounding without effort and manual models without a catalog', async () => {
    const wrapper = mount(ModelPicker, {
      props: {
        model: '',
        effort: '',
        models: [],
        showEffort: false,
        manualFallback: true,
        allowDefault: true,
        defaultOptionLabel: 'Inherit',
      },
      global: { stubs },
    })
    expect(wrapper.find('[data-testid="composer-effort-row"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="composer-model-default"]').text()).toBe('Inherit')
    await wrapper.get('input[placeholder="provider/model-id"]').setValue('custom/model')
    expect(wrapper.emitted('update:model')).toEqual([['custom/model']])
  })

  it('never changes the value on mount or disables the saved setting on missing catalog data', () => {
    const wrapper = mount(ModelPicker, {
      props: {
        model: 'custom/model',
        effort: 'high',
        models: [],
        manualFallback: true,
        inputId: 'model-setting',
        variant: 'field',
        disabled: true,
      },
      global: { stubs },
    })
    expect(wrapper.get('#model-setting').attributes('disabled')).toBeDefined()
    expect(wrapper.emitted('update:model')).toBeUndefined()
    expect(wrapper.emitted('update:effort')).toBeUndefined()
  })
})

describe('ModelPicker non-destructive selection', () => {
  it('preserves fixed effort while editing a manual model without catalog data', async () => {
    const wrapper = mount(ModelPicker, {
      props: { model: 'custom/model', effort: 'high', models: [], manualFallback: true },
      global: { stubs },
    })
    await wrapper.get('[data-testid="model-manual-input"]').setValue('custom/another-model')
    expect(wrapper.emitted('update:model')).toEqual([['custom/another-model']])
    expect(wrapper.emitted('update:effort')).toEqual([['high']])
  })

  it('does not make an unchanged model dirty or replace provider-default effort', async () => {
    const wrapper = mount(ModelPicker, {
      props: { model: 'openrouter/think', effort: '', models, effortFallback: 'model-default' },
      global: { stubs },
    })
    await wrapper.get('[data-testid="composer-model-openrouter/think"]').trigger('click')
    expect(wrapper.emitted('update:model')).toBeUndefined()
    expect(wrapper.emitted('update:effort')).toBeUndefined()
  })
})
