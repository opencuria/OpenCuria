import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

import ProviderConfigTab from './ProviderConfigTab.vue'
import * as harnessApi from '@/services/harness.api'

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    getProviderConfig: vi.fn(),
    saveProviderConfig: vi.fn(),
    deleteProviderConfig: vi.fn(),
  }
})

const getProviderConfigMock = vi.mocked(harnessApi.getProviderConfig)
const saveProviderConfigMock = vi.mocked(harnessApi.saveProviderConfig)

describe('ProviderConfigTab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    getProviderConfigMock.mockResolvedValue({
      base_url: 'https://openrouter.ai/api/v1',
      default_model: 'model-big',
      small_model: 'model-small',
      has_api_key: true,
      api_key_hint: '••••cdef',
    })
    saveProviderConfigMock.mockResolvedValue({
      base_url: 'https://openrouter.ai/api/v1',
      default_model: 'model-big',
      small_model: 'model-small',
      has_api_key: true,
      api_key_hint: '••••wxyz',
    })
  })

  it('renders provider config fields after load', async () => {
    const wrapper = mount(ProviderConfigTab)
    await flushPromises()

    expect(getProviderConfigMock).toHaveBeenCalled()
    expect(wrapper.find('#provider-api-key').exists()).toBe(true)
    expect(wrapper.find('#provider-base-url').exists()).toBe(true)
    expect(wrapper.find('#provider-default-model').exists()).toBe(true)
    expect(wrapper.find('#provider-small-model').exists()).toBe(true)
    expect((wrapper.find('#provider-default-model').element as HTMLInputElement).value).toBe(
      'model-big',
    )
  })

  it('shows masked hint and never the raw API key', async () => {
    const wrapper = mount(ProviderConfigTab)
    await flushPromises()

    expect(wrapper.text()).toContain('••••cdef')
    expect(wrapper.text()).not.toContain('sk-or-live-secret')
    expect(wrapper.html()).not.toContain('sk-or-live-secret')
  })

  it('save calls the org provider-config API', async () => {
    const wrapper = mount(ProviderConfigTab)
    await flushPromises()

    await wrapper.find('#provider-default-model').setValue('model-updated')
    await wrapper.findAll('button').find((b) => b.text().includes('Save Provider Config'))!.trigger('click')
    await flushPromises()

    expect(saveProviderConfigMock).toHaveBeenCalledWith({
      api_key: '',
      base_url: 'https://openrouter.ai/api/v1',
      default_model: 'model-updated',
      small_model: 'model-small',
    })
    expect(wrapper.text()).toContain('••••wxyz')
    expect(wrapper.text()).not.toContain('sk-or-live-secret')
  })
})
