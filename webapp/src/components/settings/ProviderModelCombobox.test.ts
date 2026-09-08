import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import ProviderModelCombobox from './ProviderModelCombobox.vue'
import type { ProviderModel } from '@/lib/harnessModels'

const models: ProviderModel[] = [
  {
    id: 'openrouter/model-big',
    name: 'Big',
    provider: 'openrouter',
    reasoning_efforts: ['high'],
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
  Command: { template: '<div><slot /></div>' },
  CommandInput: { template: '<input />' },
  CommandList: { template: '<div><slot /></div>' },
  CommandEmpty: { template: '<div><slot /></div>' },
  CommandGroup: {
    props: ['heading'],
    template:
      '<div><span v-if="heading" data-testid="group-heading">{{ heading }}</span><slot /></div>',
  },
  CommandItem: {
    template: '<button type="button" @click="$emit(\'select\')"><slot /></button>',
  },
}

function mountCombobox(modelValue: string, items: ProviderModel[] = models) {
  return mount(ProviderModelCombobox, {
    props: { modelValue, models: items, inputId: 'test-model' },
    global: { stubs },
  })
}

describe('ProviderModelCombobox', () => {
  it('groups models under provider headings', () => {
    const wrapper = mountCombobox('')

    const headings = wrapper.findAll('[data-testid="group-heading"]').map((h) => h.text())
    expect(headings).toEqual(['OpenRouter', 'ChatGPT'])
  })

  it('shows context length and effort metadata per model', () => {
    const wrapper = mountCombobox('')

    const big = wrapper.find('[data-testid="model-option-openrouter/model-big"]')
    expect(big.text()).toContain('Big')
    expect(big.text()).toContain('128k')
    expect(big.text()).toContain('High')

    const gpt = wrapper.find('[data-testid="model-option-chatgpt/gpt-5"]')
    expect(gpt.text()).toContain('GPT 5')
    expect(gpt.text()).not.toContain('128k')
  })

  it('shows the selected model name and provider in the trigger', () => {
    const wrapper = mountCombobox('openrouter/model-big')

    const trigger = wrapper.find('[data-testid="test-model-trigger"]')
    expect(trigger.text()).toContain('Big')
    expect(trigger.text()).toContain('OpenRouter')
  })

  it('emits the selected model id and supports clearing', async () => {
    const wrapper = mountCombobox('')

    await wrapper.find('[data-testid="model-option-chatgpt/gpt-5"]').trigger('click')
    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual(['chatgpt/gpt-5'])

    const clear = wrapper.findAll('button').find((b) => b.text().includes('Clear selection'))
    await clear?.trigger('click')
    expect(wrapper.emitted('update:modelValue')?.[1]).toEqual([''])
  })

  it('falls back to a manual input when no models are available', () => {
    const wrapper = mountCombobox('', [])

    expect(wrapper.find('input[placeholder="provider/model-id"]').exists()).toBe(true)
  })
})
