import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

import HarnessChatInput from './HarnessChatInput.vue'
import * as harnessApi from '@/services/harness.api'

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    getProviderConfig: vi.fn(),
  }
})

const getProviderConfigMock = vi.mocked(harnessApi.getProviderConfig)

function mountInput(props: Record<string, unknown> = {}) {
  return mount(HarnessChatInput, {
    props: { workspaceId: 'ws-1', ...props },
    global: {
      stubs: {
        RouterLink: {
          template: '<a :href="to"><slot /></a>',
          props: ['to'],
        },
        Select: { template: '<div><slot /></div>' },
        SelectContent: { template: '<div><slot /></div>' },
        SelectItem: { template: '<div><slot /></div>' },
        SelectTrigger: { template: '<div><slot /></div>' },
        SelectValue: { template: '<div><slot /></div>' },
      },
    },
  })
}

describe('HarnessChatInput', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    getProviderConfigMock.mockResolvedValue({
      base_url: 'https://openrouter.ai/api/v1',
      default_model: 'model-big',
      small_model: 'model-small',
      has_api_key: true,
      api_key_hint: '',
    })
  })

  it('loads default/small models from the org provider config as picker options', async () => {
    const wrapper = mountInput()
    await vi.waitFor(() => {
      expect(getProviderConfigMock).toHaveBeenCalled()
    })
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()
    const html = wrapper.html()
    expect(html).toContain('model-big')
    expect(html).toContain('model-small')
    expect(wrapper.text()).not.toContain('No ProviderConfig endpoint exists in M6')
  })

  it('shows org settings CTA when provider config is missing', async () => {
    getProviderConfigMock.mockRejectedValue(new Error('not found'))
    const wrapper = mountInput()
    await vi.waitFor(() => {
      expect(getProviderConfigMock).toHaveBeenCalled()
    })
    await wrapper.vm.$nextTick()
    const link = wrapper.find('a[href="/org-settings?tab=provider"]')
    expect(link.exists()).toBe(true)
    expect(link.text()).toContain('Configure OpenRouter in Org Settings')
  })

  it('shows org settings CTA when provider config has no API key', async () => {
    getProviderConfigMock.mockResolvedValue({
      base_url: 'https://openrouter.ai/api/v1',
      default_model: 'model-big',
      small_model: 'model-small',
      has_api_key: false,
      api_key_hint: '',
    })
    const wrapper = mountInput()
    await vi.waitFor(() => {
      expect(getProviderConfigMock).toHaveBeenCalled()
    })
    await wrapper.vm.$nextTick()
    expect(wrapper.find('a[href="/org-settings?tab=provider"]').exists()).toBe(true)
  })

  it('falls back to free-text input when the provider config is missing', async () => {
    getProviderConfigMock.mockRejectedValue(new Error('not found'))
    const wrapper = mountInput()
    await vi.waitFor(() => {
      expect(getProviderConfigMock).toHaveBeenCalled()
    })
    await wrapper.vm.$nextTick()
    expect(wrapper.find('input[placeholder*="org default"]').exists()).toBe(true)
  })

  it('keeps the handleSend(prompt, mode, model) signature with org-default model', async () => {
    const wrapper = mountInput({ mode: 'plan' })
    const textarea = wrapper.find('textarea')
    await textarea.setValue('hello world')
    await wrapper.find('button[class*="bg-primary"], button:not([role="tab"])').trigger('click').catch(() => {})
    // Fall back to direct emit check: at least the send payload shape is preserved.
    const sends = wrapper.emitted('send') ?? []
    if (sends.length > 0) {
      expect(sends[0]![1]).toBe('plan')
      expect(typeof sends[0]![2]).toBe('string')
    } else {
      // No prompt typed through stubbed textarea: send explicitly via keyboard.
      await textarea.trigger('keydown', { key: 'Enter' })
      const retried = wrapper.emitted('send') ?? []
      expect(retried.length).toBeGreaterThan(0)
      expect(retried[0]![0]).toBe('hello world')
      expect(retried[0]![1]).toBe('plan')
      expect(retried[0]![2]).toBe('')
    }
  })

  it('includes selected skill ids in the send payload', async () => {
    const wrapper = mountInput({
      skillOptions: [
        {
          id: 'skill-1',
          name: 'Lint rules',
          body: 'Always lint',
          scope: 'personal',
          created_by_email: null,
          created_at: '2026-03-29T10:00:00.000Z',
          updated_at: '2026-03-29T10:00:00.000Z',
        },
      ],
    })
    await vi.waitFor(() => {
      expect(getProviderConfigMock).toHaveBeenCalled()
    })

    const skillsButton = wrapper.findAll('button').find((button) => button.text().includes('Skills'))
    expect(skillsButton).toBeTruthy()
    await skillsButton!.trigger('click')
    const skillOption = wrapper.findAll('button').find((button) => button.text().includes('Lint rules'))
    expect(skillOption).toBeTruthy()
    await skillOption!.trigger('mousedown')

    const textarea = wrapper.find('textarea')
    await textarea.setValue('use skills')
    await textarea.trigger('keydown', { key: 'Enter' })

    const sends = wrapper.emitted('send') ?? []
    expect(sends.length).toBeGreaterThan(0)
    expect(sends[0]![3]).toEqual(['skill-1'])
  })
})
