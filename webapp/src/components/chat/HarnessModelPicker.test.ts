import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import HarnessModelPicker from './HarnessModelPicker.vue'
import type { ProviderModel } from '@/lib/harnessModels'

const models: ProviderModel[] = [
  {
    id: 'acme/think',
    name: 'Think',
    reasoning_efforts: ['low', 'high'],
    default_effort: 'high',
    supports_tools: true,
    context_length: 128_000,
    max_output_tokens: 8_192,
  },
  {
    id: 'acme/plain',
    name: 'Plain',
    reasoning_efforts: [],
    default_effort: '',
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
  DropdownMenuSeparator: { template: '<hr />' },
}

describe('HarnessModelPicker', () => {
  it('lists Auto plus catalog models and hides Fast', () => {
    const wrapper = mount(HarnessModelPicker, {
      props: { model: 'acme/think', effort: 'high', models },
      global: { stubs },
    })
    expect(wrapper.text()).toContain('Auto')
    expect(wrapper.text()).toContain('Think')
    expect(wrapper.text()).toContain('Plain')
    expect(wrapper.text()).not.toContain('Fast')
    expect(wrapper.find('[data-testid="composer-effort-row"]').exists()).toBe(true)
  })

  it('shows model name and effort on the compact trigger', () => {
    const wrapper = mount(HarnessModelPicker, {
      props: { model: 'acme/think', effort: 'high', models },
      global: { stubs },
    })
    const trigger = wrapper.find('[data-testid="composer-model-trigger"]')
    expect(trigger.text()).toContain('Think')
    expect(trigger.text()).toContain('High')
  })

  it('hides the effort submenu when the model has no reasoning', () => {
    const wrapper = mount(HarnessModelPicker, {
      props: { model: 'acme/plain', effort: '', models },
      global: { stubs },
    })
    expect(wrapper.find('[data-testid="composer-effort-row"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="composer-model-trigger"]').text()).toContain('Plain')
    expect(wrapper.find('[data-testid="composer-model-trigger"]').text()).not.toContain('High')
  })

  it('filters the catalog by the search query', async () => {
    const wrapper = mount(HarnessModelPicker, {
      props: { model: '', effort: '', models },
      global: { stubs },
    })
    await wrapper.find('[data-testid="composer-model-search"]').setValue('plain')
    expect(wrapper.text()).toContain('Plain')
    expect(wrapper.text()).not.toContain('Think')
  })
})
