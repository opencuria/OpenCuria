import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import ProviderModelCombobox from './ProviderModelCombobox.vue'
import type { ProviderModel } from '@/lib/harnessModels'

const models: ProviderModel[] = [
  {
    id: 'openrouter/model-big',
    name: 'Big',
    provider: 'openrouter',
    reasoning_efforts: ['low', 'high'],
    default_effort: 'high',
    supports_tools: true,
    context_length: 128_000,
    max_output_tokens: 8_192,
  },
  {
    id: 'chatgpt/gpt-5',
    name: 'GPT 5',
    provider: 'chatgpt',
    reasoning_efforts: [],
    default_effort: '',
    supports_tools: true,
    context_length: 0,
    max_output_tokens: 0,
  },
]

const stubs = {
  Popover: { template: '<div><slot /></div>' },
  PopoverTrigger: { template: '<div><slot /></div>' },
  PopoverContent: { template: '<div><slot /></div>' },
}

function mountCombobox(
  modelValue: string,
  items: ProviderModel[] = models,
  effort = '',
) {
  return mount(ProviderModelCombobox, {
    props: { modelValue, models: items, effort, inputId: 'test-model' },
    global: { stubs },
  })
}

function manyModels(count: number): ProviderModel[] {
  return Array.from({ length: count }, (_, i) => ({
    id: `openrouter/model-${i}`,
    name: `Model ${i}`,
    provider: 'openrouter',
    reasoning_efforts: [],
    default_effort: '',
    supports_tools: true,
    context_length: 0,
    max_output_tokens: 0,
  }))
}

describe('ProviderModelCombobox', () => {
  it('groups models under provider headings', () => {
    const wrapper = mountCombobox('')

    const headings = wrapper.findAll('[data-testid="group-heading"]').map((h) => h.text())
    expect(headings).toEqual(['OpenRouter', 'ChatGPT'])
  })

  it('wraps full model names without context-length metadata', () => {
    const wrapper = mountCombobox('')

    const big = wrapper.find('[data-testid="model-option-openrouter/model-big"]')
    expect(big.text()).toContain('Big')
    expect(big.text()).not.toContain('128k')
    expect(big.attributes('title')).toBe('openrouter/model-big')
  })

  it('shows the selected model name, provider, and effort in the trigger', () => {
    const wrapper = mountCombobox('openrouter/model-big', models, 'high')

    const trigger = wrapper.find('[data-testid="test-model-trigger"]')
    expect(trigger.text()).toContain('Big')
    expect(trigger.text()).toContain('OpenRouter')
    expect(trigger.text()).toContain('High')
    expect(trigger.attributes('title')).toContain('Big')
  })

  it('emits the selected model id and supports clearing', async () => {
    const wrapper = mountCombobox('')

    await wrapper.find('[data-testid="model-option-chatgpt/gpt-5"]').trigger('click')
    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual(['chatgpt/gpt-5'])

    const clear = wrapper.findAll('button').find((b) => b.text().includes('Clear selection'))
    await clear?.trigger('click')
    expect(wrapper.emitted('update:modelValue')?.[1]).toEqual([''])
  })

  it('only shows an effort select for reasoning models', () => {
    const wrapper = mountCombobox('')

    expect(
      wrapper.find('[data-testid="model-effort-openrouter/model-big"]').exists(),
    ).toBe(true)
    expect(wrapper.find('[data-testid="model-effort-chatgpt/gpt-5"]').exists()).toBe(false)
  })

  it('emits the snapped effort when selecting a model', async () => {
    const wrapper = mountCombobox('', models, '')

    await wrapper.find('[data-testid="model-option-openrouter/model-big"]').trigger('click')
    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual(['openrouter/model-big'])
    expect(wrapper.emitted('update:effort')?.[0]).toEqual(['high'])
  })

  it('emits update:effort and the model id when the row effort changes', async () => {
    const wrapper = mountCombobox('', models, '')

    const select = wrapper.find('[data-testid="model-effort-openrouter/model-big"]')
    await select.setValue('low')

    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual(['openrouter/model-big'])
    expect(wrapper.emitted('update:effort')?.[0]).toEqual(['low'])
  })

  it('caps rendered rows and hints at refining the search', async () => {
    const wrapper = mountCombobox('', manyModels(200))

    const more = wrapper.find('[data-testid="model-list-more"]')
    expect(more.exists()).toBe(true)
    expect(more.text()).toContain('150')
    expect(more.text()).toContain('200')

    await wrapper.find('[data-testid="model-search"]').setValue('model-199')
    expect(wrapper.find('[data-testid="model-option-openrouter/model-199"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="model-list-more"]').exists()).toBe(false)
  })

  it('falls back to a manual input when no models are available', () => {
    const wrapper = mountCombobox('', [])

    expect(wrapper.find('input[placeholder="provider/model-id"]').exists()).toBe(true)
  })
})
